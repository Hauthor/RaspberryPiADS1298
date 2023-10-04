"""
# file: ADS1298_API.py
# author: Frederic Simard (frederic.simard.1@outlook.com) for ADS1299 driver
          Torfinn Berset (torfinn@bloomlife.com) ported to ADS1298
# version: Fall 2023
# descr: This files implements the basic features required to operate the ADS1298 using the SPI port
         of a Raspberry Pi (tested on RPi 4b).
         
         The API handles the communication over the SPI port and uses a separate thread - managed by GPIO - 
         to process samples sent by the ADS1298. Samples received are pushed to a registered callback in 
         the form of a numpy Array with a length equal to the number of channels (think of observer pattern).
         
         A default Callback that prints out values on screen is provided in this file and registered in the test script.
         
         A stubbed mode is also available to develop with the API offline, in that mode random numbers are
         returned at a rate close to the defined sampling rate. Stubbed mode becomes active whenever spidev
         cannot be imported properly. 
         
         Public methods overview:
         
             Basic operations:
                - init, initialise the API
                - openDevice, open SPI, power-up/reset sequence the ADS1298 and push default configuration
                - closeDevice, close SPI, power down ADS1298
                
            Configuration:
                - configure, is the public interface to change system configuration. It uses optional parameters
                        - nb_channels, sets the number of channels {1,8}, default 8
                        - sampling_rate, sets the sampling rate {500,1000,2000, 4000}, default 500
                        - bias_enabled, used to enable/disable Bias drive {True,False}, default True
                    Note: changing any option will interrupt any active stream
                    Note: 2000Hz sampling rate is unstable, it requires the 24 bits conversion to be done in a different thread
                    Note: gain is set to 12 and is not configurable
                
                - registerClient, add a callback to use for data
                
            Control:
                - startEegStreaming, starts streaming of eeg data using active configuration
                - startTestStream, starts streaming of test data (generated by ADS1298)
                - stopStreaming, stop any ongoing stream
                - reset ADS1298, toggle reset pin on ADS1298
            
         Hardware configuration:
            The Raspberry Pi 4b is used as a reference
            
                Signal  |  RPi GPIO |  ADS Pin
                --------------------------------
                MOSI    |     20    |    DIN
                MISO    |     19    |    DOUT
                SCLK    |     21    |    SCLK
                CS      |     24    |    CS
                --------------------------------
                START   |     22    |    START
                RESET   |     24    |    nRESET
                PWRDN   |     25    |    nPWRDN
                DRDY    |     23    |    DRDY
                CE1     |     7     |    CE1
         
            The pins for the SPI port cannot be changed. CS can be flipped, if using /dev/spidev0.1 instead.
            The GPIOS can be reaffected.
            
  Requirements and setup:
    - numpy:  https://scipy.org/install.html
    - spidev:  https://pypi.python.org/pypi/spidev
    - how to configure SPI on raspberry Pi: https://www.raspberrypi.org/documentation/hardware/raspberrypi/spi/README.md
"""

import struct
from threading import Lock, Thread
from time import sleep

import numpy as np

STUB_API = False
try:
    import spidev
    import RPi.GPIO as GPIO
except ImportError:
    STUB_API = True

# exg data scaling function
SCALE_TO_UVOLT = (5 / 12) / (2 ** 24)  # TODO: verify
NUM_CHANNELS = 8

"""
# conv24bitsToFloat(unpacked)
# @brief utility function that converts signed 24 bits integer to scaled floating point
#        the 24 bits representation needs to be provided as a 3 bytes array MSB first
# @param unpacked (bytes array) 24 bits data point
# @return data scaled to uVolt
# @thanks: https://github.com/OpenBCI/OpenBCI_Python/blob/master/open_bci_ganglion.py
"""


def convert_24b_data(unpacked, fmt=">i"):
    """Convert 24bit data coded on 3 bytes to a proper integer"""
    if len(unpacked) != 3:
        raise ValueError("Input should be 3 bytes long.")

    literal_read = struct.pack("3B", *unpacked)

    # 3byte int in 2s compliment
    prefix = b"\xff" if unpacked[0] > 127 else b"\x00"

    # unpack little endian(>) signed integer(i) (makes unpacking platform independent)
    return struct.unpack(fmt, prefix + literal_read)[0]


def convert_24b_to_float(unpacked):
    return convert_24b_data(unpacked) * SCALE_TO_UVOLT


"""
DefaultCallback
@brief used as default client callback for tests 
@data byte array of 1xN, where N is the number of channels
"""


def default_callback(raw):
    GPIO.output(CE1, GPIO.HIGH)
    samples = np.zeros(NUM_CHANNELS)
    status_word = convert_24b_data(raw[0:3], ">I") & 0xffffff

    assert status_word >> 20 == 0b1100, "Data stream out of sync"

    loff_stat_p = (status_word >> 12) & 0xff
    loff_stat_n = (status_word >> 4) & 0xff

    for i in range(0, NUM_CHANNELS):
        samples[i] = convert_24b_to_float(raw[(i * 3 + 3): ((i + 1) * 3 + 3)])

    print(f"LOFF P{loff_stat_p:08b} N{loff_stat_n:08b}")
    print(f"samples: {samples}")


""" ADS1298 registers map """
REG_ID = 0x00
REG_CONFIG1 = 0x01
REG_CONFIG2 = 0x02
REG_CONFIG3 = 0x03
REG_CONFIG4 = 0x17
REG_LOFF = 0x04
REG_CHnSET_BASE = 0x05
REG_BIAS_SENSP = 0x0D
REG_BIAS_SENSN = 0x0E
REG_LOFF_SENSP = 0x0F
REG_LOFF_SENSN = 0x10
REG_LOFF_FLIP = 0x11
REG_WCT1 = 0x18
REG_WCT2 = 0x19

""" ADS1298 Commands """
WAKEUP = 0x02
STANDBY = 0x04
RESET = 0x06
START = 0x08
STOP = 0x0A
RDATAC = 0x10
SDATAC = 0x11
RDATA = 0x12

""" Rconfigurable pin mapping """
START_PIN = 22
nRESET_PIN = 24
nPWRDN_PIN = 25
DRDY_PIN = 23
CE1 = 7

"""
# Ads1298Api
# @brief Encapsulated API, provides basic functionalities
#        to configure and control a ADS1298 connected to the SPI port
"""
class Ads1298Api:
    # spi device
    spi = None

    # thread processing inputs
    stubThread = None
    APIAlive = True

    # lock over SPI port
    spi_lock = None

    # array of client handles
    clientUpdateHandles = []

    # device configuration
    nb_channels = 8  # {1-8}
    sampling_rate = 500  # {500,1000,2000,4000}
    bias_enabled = False  # {True, False}

    # True when a data stream is active
    stream_active = False

    # Spi clk frequency [Hz]
    spi_speed = 5000000
    # Delay before releasing CS in [us]
    spi_pause = 3

    # This mirrors the register state on the ADS1298
    config_registers: dict[int, int] = dict()

    """ PUBLIC
    # Constructor
    # @brief
    """

    def __init__(self):
        if not STUB_API:
            self.spi = spidev.SpiDev()

    def __del__(self):
        self.close_device()

    """ PUBLIC
    # openDevice
    # @brief open the ADS1298 interface and initialize the chip
    """

    def open_device(self):
        if not STUB_API:
            # open and configure SPI port
            self.spi.open(0, 1)
            self.spi.max_speed_hz = self.spi_speed
            self.spi.mode = 0b01  # SPI settings are CPOL = 0 and CPHA = 1.

            # using BCM pin numbering scheme
            GPIO.setmode(GPIO.BCM)

            # setup control pins
            GPIO.setup(START_PIN, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(nRESET_PIN, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(nPWRDN_PIN, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(CE1, GPIO.OUT, initial=GPIO.HIGH)

            # setup DRDY callback
            GPIO.setup(DRDY_PIN, GPIO.IN)
            GPIO.add_event_detect(DRDY_PIN, GPIO.FALLING, callback=self.drdy_callback)

        else:
            # setup fake data generator
            print("stubbed mode")
            self.stubThread = Thread(target=self.stub_task)
            self.stubThread.start()

        # spi port mutex
        self.spi_lock = Lock()

        # init the ADS1298
        self.ads1298_startup_sequence()

    """ PUBLIC
    # closeDevice
    # @brief close and clean up the SPI, GPIO and running thread
    """

    def close_device(self):
        self.APIAlive = False

        if STUB_API:
            self.stubThread.join()
        else:
            self.spi.close()
            GPIO.cleanup()

    """ PUBLIC
    # startEegStream
    # @brief Init an eeg data stream
    """

    def start_exg_stream(self):
        # stop any ongoing stream
        self.reset_ongoing_state()

        # setup ExG mode
        self.setup_exg_mode()

        # start the stream
        self.stream_active = True
        self.set_start(True)
        self.spi_transmit_byte(RDATAC)

    """ PUBLIC
    # startTestStream
    # @brief Init a test data stream
    """

    def start_test_stream(self):
        # stop any ongoing stream
        self.reset_ongoing_state()

        # setup test mode
        self.setup_test_mode()

        # start the stream
        self.stream_active = True
        self.set_start(True)
        self.spi_transmit_byte(RDATAC)

    """ PUBLIC
    # stopStream
    # @brief shut down any active stream
    """

    def stop_stream(self):
        # stop any ongoing ADS stream
        self.spi_transmit_byte(SDATAC)
        self.stream_active = False
        self.APIAlive = False

    """ PUBLIC
    # registerClient
    # @brief register a client handle to push data
    # @param clientHandle, update handle of the client
    """

    def register_client(self, client_handle):
        self.clientUpdateHandles.append(client_handle)

    """ PUBLIC
    # configure
    # @brief provide the ADS1298 configuration interface, it uses optional parameters
    #        no parameter validation take place, make sure to provide valid value
    #   - sampling_rate {500, 1000, 2000, 4000}
    #   - bias_enabled {True, False}
    """

    def configure(self, sampling_rate=None, bias_enabled=None):
        assert not self.stream_active

        if sampling_rate is not None:
            self.sampling_rate = sampling_rate

        if bias_enabled is not None:
            self.bias_enabled = bias_enabled

    # %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
    #   ADS1298 control
    # %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

    """ PRIVATE
    # ADS1298StartupSequence
    # @brief start-up sequence to init the chip
    """

    def ads1298_startup_sequence(self):
        # pwr and reset goes up
        self.set_nreset(True)
        self.set_npwrdn(True)

        # wait
        sleep(1)

        # toggle reset
        self.toggle_reset()

        # send SDATAC
        self.reset_ongoing_state()

        # Check type of ADS
        self.check_device_id()

    """ PRIVATE
    # setupEEGMode
    # @brief setup EEG mode for data streaming
    """

    def setup_exg_mode(self):
        print("Setting up ExG mode")

        self.spi_write_single_reg(REG_CONFIG2, 0x00)
        # Gain of 12
        self.configure_all_channels(0x60)

        self.spi_write_single_reg(REG_BIAS_SENSP, 0xFF)
        self.spi_write_single_reg(REG_BIAS_SENSN, 0x01)

        #Enable WCTA, channel 2 positive
        self.spi_write_single_reg(REG_WCT1, 0xA)

        self.configure_dc_leads_off(True)
        self.setup_bias_drive()

    def configure_dc_leads_off(self, enable: bool):
        if enable:
            self.spi_write_single_reg(REG_LOFF, 0x93)
            self.spi_write_single_reg(REG_LOFF_SENSP, 0xFF)
            self.spi_write_single_reg(REG_LOFF_SENSN, 0xFF)
            self.spi_write_single_reg(REG_LOFF_FLIP, 0xFF)
            self.spi_write_single_reg(REG_CONFIG4, 0x02)
        else:
            self.spi_write_single_reg(REG_LOFF, 0x00)
            self.spi_write_single_reg(REG_LOFF_SENSP, 0x00)
            self.spi_write_single_reg(REG_LOFF_SENSN, 0x00)
            self.spi_write_single_reg(REG_LOFF_FLIP, 0x00)
            self.spi_write_single_reg(REG_CONFIG4, 0x00)

    """ PRIVATE
    # setupTestMode
    # @brief setup TEST mode for data streaming
    """

    def setup_test_mode(self):
        print("Setting up Test Mode")
        # stop any ongoing ads stream
        self.spi_transmit_byte(SDATAC)

        # Write CONFIG2 D0h
        self.spi_write_single_reg(REG_CONFIG2, 0x11)
        # Internal reference 
        self.spi_write_single_reg(REG_CONFIG3, 0xC0)
        self.configure_dc_leads_off(False)

        # Write CHnSET 05h (connects test signal)
        self.configure_all_channels(0x05)

    """ PRIVATE
    # resetOngoingState
    # @brief reset the registers configuration
    """

    def reset_ongoing_state(self):
        # send SDATAC
        self.spi_transmit_byte(SDATAC)

        # setup CONFIG3 register
        self.spi_write_single_reg(REG_CONFIG3, 0x48)

        # setup CONFIG1 register
        self.set_sampling_rate()

        # setup CONFIG2 register
        self.spi_write_single_reg(REG_CONFIG2, 0xC0)

        # disable any bias
        self.spi_write_single_reg(REG_BIAS_SENSP, 0x00)
        self.spi_write_single_reg(REG_BIAS_SENSN, 0x00)

        # input shorted
        self.configure_all_channels(0x01)

    """ PRIVATE
    # setSamplingRate
    # @brief set CONFIG1 register, which defines the sampling rate
    """

    def set_sampling_rate(self):
        temp_reg_value = 0x80  # base value

        # chip in sampling rate
        if self.sampling_rate == 4000:
            temp_reg_value |= 0b011
        if self.sampling_rate == 2000:
            temp_reg_value |= 0b100
        elif self.sampling_rate == 1000:
            temp_reg_value |= 0b101
        elif self.sampling_rate == 500:
            temp_reg_value |= 0b110
        else:
            raise ValueError("Invalid sample rate")

        self.spi_write_single_reg(REG_CONFIG1, temp_reg_value)

    """ PRIVATE
    # setupBiasDrive
    # @brief enable the bias drive by configuring the appropriate registers
    # @ref ADS1298 datasheet
    """

    def setup_bias_drive(self):
        if not self.bias_enabled:
            return

        print("Configuring bias registers")
        reg_value = (2 ** NUM_CHANNELS) - 1
        self.spi_write_single_reg(REG_BIAS_SENSP, reg_value)
        self.spi_write_single_reg(REG_BIAS_SENSN, reg_value)
        self.spi_write_single_reg(REG_CONFIG3, 0xEC)

    """ PRIVATE
    # stubTask
    # @brief activated in stub mode, will generate fake data
    """

    def stub_task(self):
        while self.APIAlive:
            if self.stream_active:
                raw = b"\xC0" + np.random.bytes(2 + 3 * NUM_CHANNELS)[0:]
                for handle in self.clientUpdateHandles:
                    handle(raw)
            sleep(1.0 / float(self.sampling_rate))

    def check_device_id(self):
        res = self.spi_read_reg(REG_ID)
        assert res & 0x18 == 0x10, "Reserved bytes don't match"
        assert res & 0xE0 == 0x80, "Not ADS129x device family"
        assert res & 0x03 == 0x02, "Not ADS1298 device"

    # %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
    #   GPIO Interface
    # %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

    """ PRIVATE
    # drdy_callback
    # @brief callback triggered on DRDY falling edge. When this happens, if the stream
             is active, will get all the sample from the ADS1298 and update all
             clients
    # @param state, state of the pin to read (not used)
    """

    def drdy_callback(self, state):
        # on event, read the data from ADS

        if not self.stream_active:
            return

        # read 24 + n*24 bits or 3+n*3 bytes
        raw = self.spi_read_multiple_bytes(3 + NUM_CHANNELS * 3)

        # broadcast raw
        for handle in self.clientUpdateHandles:
            handle(raw)

    """ PRIVATE
    # setStart
    # @brief control the START pin
    # @param state, state of the pin to set
    """

    def set_start(self, state):
        self.set_pin(self.START_PIN, state)

    """ PRIVATE
    # toggleReset
    # @brief toggle the nRESET pin while respecting the timing
    """

    def toggle_reset(self):
        # toggle reset
        self.set_nreset(False)
        sleep(0.2)
        self.set_nreset(True)
        sleep(0.2)

    """ PRIVATE
    # setnReset
    # @brief control the nRESET pin
    # @param state, state of the pin to set
    """

    def set_nreset(self, state):
        self.set_pin(self.nRESET_PIN, state)

    """ PRIVATE
    # setnPWRDN
    # @brief control the nPWRDN pin
    # @param state, state of the pin to set
    """

    def set_npwrdn(self, state: bool):
        self.set_pin(self.nPWRDN_PIN, state)

    def configure_all_channels(self, config: int):
        self.spi_write_multiple_reg(REG_CHnSET_BASE, [config] * NUM_CHANNELS)

    def set_pin(self, pin: int, state: bool):
        if STUB_API:
            return

        GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)

    # %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
    #   SPI Interface
    # %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

    """ PRIVATE
    # SPI_transmitByte
    # @brief push a single byte on the SPI port
    # @param byte, value to push on the port
    """

    def spi_transmit_byte(self, byte):
        if STUB_API:
            return

        with self.spi_lock:
            self.spi.xfer([byte], self.spi_speed, self.spi_pause)

    """ PRIVATE
    # SPI_writeSingleReg
    # @brief write a value to a single register
    # @param reg, register address to write to
    # @param byte, value to write
    """

    def spi_write_single_reg(self, reg, byte):
        self.config_registers[reg] = byte

        if STUB_API:
            return

        with self.spi_lock:
            self.spi.xfer([reg | 0x40, 0x00, byte], self.spi_speed, self.spi_pause)

    """ PRIVATE
    # SPI_writeMultipleReg
    # @brief write a series of values to a series of adjacent registers
    #        the number of adjacent registers to write is defined by the length
    #        of the value array
    # @param start_reg, base address from where to start writing
    # @param byte_array, array of bytes containing registers values
    """

    def spi_write_multiple_reg(self, start_reg: int, byte_array: list[int]):
        for index, addr in enumerate(range(start_reg, start_reg + len(byte_array))):
            self.config_registers[addr] = byte_array[index]

        if STUB_API:
            return

        with self.spi_lock:
            self.spi.xfer([start_reg | 0x40, len(byte_array) - 1] + byte_array, self.spi_speed, self.spi_pause)

    """ PRIVATE
    # SPI_readMultipleBytes
    # @brief read multiple bytes from the SPI port
    # @param nb_bytes, nb of bytes to read
    """

    def spi_read_multiple_bytes(self, nb_bytes):
        if STUB_API:
            return []

        with self.spi_lock:
            return self.spi.xfer([0x00] * nb_bytes, self.spi_speed, self.spi_pause)

    def spi_read_reg(self, reg):
        if STUB_API:
            return 0x92 if reg == REG_ID else 0x00

        with self.spi_lock:
            r = self.spi.xfer([0x20 | reg, 0x00, 0x00], self.spi_speed, self.spi_pause)
            return r[2]


def _test(use_test_signal=False):
    print("Starting validation sequence")

    # init ads api
    ads = Ads1298Api()

    # init device
    ads.open_device()
    # attach default callback
    ads.register_client(default_callback)
    # configure ads
    ads.configure(sampling_rate=500)

    print("ADS1298 API test stream starting")

    # begin test streaming
    ads.start_test_stream() if use_test_signal else ads.start_exg_stream()

    # wait
    sleep(1)

    print("ADS1298 API test stream stopping")

    # stop device
    ads.stop_stream()
    # clean up
    ads.close_device()

    sleep(1)
    print("Test Over")


if __name__ == "__main__":
    try:
        _test(True)
    except Exception as e:
        print(e)
        GPIO.cleanup()
