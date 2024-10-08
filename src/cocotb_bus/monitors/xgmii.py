# Copyright cocotb contributors
# Copyright (c) 2013 Potential Ventures Ltd
# Licensed under the Revised BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-3-Clause

"""Monitor for XGMII (10 Gigabit Media Independent Interface)."""

# By default cast to scapy packets, otherwise we pass the string of bytes
try:
    from scapy.all import Ether
    _have_scapy = True
except ImportError:
    _have_scapy = False

import struct
import zlib

from scapy.utils import hexdump

from cocotb.triggers import RisingEdge

from cocotb_bus.compat import convert_binary_to_unsigned
from cocotb_bus.monitors import Monitor

_XGMII_IDLE      = 0x07  # noqa
_XGMII_START     = 0xFB  # noqa
_XGMII_TERMINATE = 0xFD  # noqa

_PREAMBLE_SFD = b"\x55\x55\x55\x55\x55\x55\xD5"


class XGMII(Monitor):
    """XGMII (10 Gigabit Media Independent Interface) Monitor.

    Assumes a single vector, either 4 or 8 bytes plus control bit for each byte.

    If interleaved is ``True`` then the control bits are adjacent to the bytes.

    .. versionchanged:: 1.4.0
        This now emits packets of type :class:`bytes` rather than :class:`str`,
        which matches the behavior of :class:`cocotb.drivers.xgmii.XGMII`.
    """

    def __init__(self, signal, clock, interleaved=True, callback=None,
                 event=None):
        """Args:
            signal (SimHandle): The XGMII data bus.
            clock (SimHandle): The associated clock (assumed to be
                driven by another coroutine).
            interleaved (bool, optional): Whether control bits are interleaved
                with the data bytes or not.

        If interleaved the bus is
            byte0, byte0_control, byte1, byte1_control, ...

        Otherwise expect
            byte0, byte1, ..., byte0_control, byte1_control, ...
        """
        self.log = signal._log
        self.clock = clock
        self.signal = signal
        self.bytes = len(self.signal) // 9
        self.interleaved = interleaved
        Monitor.__init__(self, callback=callback, event=event)

    def _get_bytes(self):
        """Take a value and extract the individual bytes and control bits.

        Returns a tuple of lists.
        """
        value = convert_binary_to_unsigned(self.signal.value)
        bytes = []
        ctrls = []
        byte_shift = 8
        ctrl_base = 8 * self.bytes
        ctrl_inc = 1
        if self.interleaved:
            byte_shift += 1
            ctrl_base = 8
            ctrl_inc = 9

        for i in range(self.bytes):
            bytes.append((value >> (i * byte_shift)) & 0xff)
            ctrls.append(bool(value & (1 << ctrl_base)))
            ctrl_base += ctrl_inc

        return ctrls, bytes

    def _add_payload(self, ctrl, bytes):
        """Take the payload and return true if more to come"""
        for index, byte in enumerate(bytes):
            if ctrl[index]:
                if byte != _XGMII_TERMINATE:
                    self.log.error("Got control character in XGMII payload")
                    self.log.info("data = :" +
                                  " ".join(["%02X" % b for b in bytes]))
                    self.log.info("ctrl = :" +
                                  " ".join(["%s" % str(c) for c in ctrl]))
                    self._pkt = bytearray()
                return False

            self._pkt.append(byte)
        return True

    async def _monitor_recv(self):
        clk = RisingEdge(self.clock)
        self._pkt = bytearray()

        while True:
            await clk
            ctrl, bytes = self._get_bytes()

            if ctrl[0] and bytes[0] == _XGMII_START:

                ctrl, bytes = ctrl[1:], bytes[1:]

                while self._add_payload(ctrl, bytes):
                    await clk
                    ctrl, bytes = self._get_bytes()

            elif self.bytes == 8 :
                if ctrl[4] and bytes[4] == _XGMII_START:

                    ctrl, bytes = ctrl[5:], bytes[5:]

                    while self._add_payload(ctrl, bytes):
                        await clk
                        ctrl, bytes = self._get_bytes()

            if self._pkt:

                self.log.debug("Received:\n%s" % (hexdump(self._pkt, dump=True)))

                if len(self._pkt) < 64 + 7:
                    self.log.error("Received a runt frame!")
                if len(self._pkt) < 12:
                    self.log.error("No data to extract")
                    self._pkt = bytearray()
                    continue

                preamble_sfd = self._pkt[0:7]
                crc32 = self._pkt[-4:]
                payload = self._pkt[7:-4]

                if preamble_sfd != _PREAMBLE_SFD:
                    self.log.error("Got a frame with unknown preamble/SFD")
                    self.log.error(hexdump(preamble_sfd, dump=True))
                    self._pkt = bytearray()
                    continue

                expected_crc = struct.pack("<I",
                                           (zlib.crc32(payload) & 0xFFFFFFFF))

                if crc32 != expected_crc:
                    self.log.error("Incorrect CRC on received packet")
                    self.log.info("Expected: %s" % (hexdump(expected_crc, dump=True)))
                    self.log.info("Received: %s" % (hexdump(crc32, dump=True)))

                # Use scapy to decode the packet
                if _have_scapy:
                    p = Ether(payload)
                    self.log.debug("Received decoded packet:\n%s" % p.show2())
                else:
                    p = payload

                self._recv(p)
                self._pkt = bytearray()
