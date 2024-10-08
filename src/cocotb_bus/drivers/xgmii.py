# Copyright cocotb contributors
# Copyright (c) 2013 Potential Ventures Ltd
# Licensed under the Revised BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-3-Clause

"""Drivers for XGMII (10 Gigabit Media Independent Interface)."""

import struct
import zlib

from scapy.utils import hexdump

from cocotb.triggers import RisingEdge
from cocotb.handle import SimHandleBase

from cocotb_bus.drivers import Driver
from cocotb_bus.compat import create_binary

_XGMII_IDLE      = 0x07  # noqa
_XGMII_START     = 0xFB  # noqa
_XGMII_TERMINATE = 0xFD  # noqa

# Preamble is technically supposed to be 7 bytes of 0x55 but it seems that it's
# permissible for the start byte to replace one of the preamble bytes
# see http://grouper.ieee.org/groups/802/3/10G_study/email/msg04647.html
_PREAMBLE_SFD = b"\x55\x55\x55\x55\x55\x55\xD5"


class _XGMIIBus:
    r"""Helper object for abstracting the underlying bus format.

    Index bytes directly on this object, pass a tuple of ``(value, ctrl)`` to
    set a byte.

    For example:

    >>> xgmii = _XGMIIBus(4)
    >>> xgmii[0] = (_XGMII_IDLE, True)  # Control byte
    >>> xgmii[1] = (b"\x55", False)      # Data byte
    """

    def __init__(self, nbytes: int, interleaved: bool = True):
        """Args:
            nbytes: The number of bytes transferred per clock cycle
                (usually 8 for SDR, 4 for DDR).

            interleaved: The arrangement of control bits on the bus.

                If interleaved we have a bus with 9-bits per
                byte, the control bit being the 9th bit of each
                byte.

                If not interleaved then we have a byte per data
                byte plus a control bit per byte in the MSBs.
        """

        self._integer = 0
        self._interleaved = interleaved
        self._nbytes = nbytes

    def __setitem__(self, index, value):
        byte, ctrl = value

        if isinstance(byte, bytes):
            byte = ord(byte)

        if index >= self._nbytes:
            raise IndexError("Attempt to access byte %d of a %d byte bus" % (
                index, self._nbytes))

        if self._interleaved:
            self._integer |= (byte << (index * 9))
            self._integer |= (int(ctrl) << (9*index + 8))
        else:
            self._integer |= (byte << (index * 8))
            self._integer |= (int(ctrl) << (self._nbytes*8 + index))

    @property
    def value(self):
        """Get the integer representation of this data word suitable for driving
        onto the bus.

        NB clears the value.
        """
        value = create_binary(self._integer, self._nbytes * 9, big_endian=False)
        self._integer = 0
        return value

    def __len__(self):
        return self._nbytes


class XGMII(Driver):
    """XGMII (10 Gigabit Media Independent Interface) driver."""

    def __init__(self, signal: SimHandleBase, clock: SimHandleBase, interleaved: bool = True):
        """Args:
            signal: The XGMII data bus.
            clock: The associated clock (assumed to be
                driven by another coroutine).
            interleaved: Whether control bits are interleaved
                with the data bytes or not.

        If interleaved the bus is
            byte0, byte0_control, byte1, byte1_control, ...

        Otherwise expect
            byte0, byte1, ..., byte0_control, byte1_control, ...
        """
        self.log = signal._log
        self.signal = signal
        self.clock = clock
        self.bus = _XGMIIBus(len(signal)//9, interleaved=interleaved)
        Driver.__init__(self)
        self.idle()

    @staticmethod
    def layer1(packet: bytes) -> bytes:
        """Take an Ethernet packet (as a string) and format as a layer 1 packet.

        Pad to 64 bytes, prepend preamble and append 4-byte CRC on the end.

        Args:
            packet: The Ethernet packet to format.

        Returns:
            The formatted layer 1 packet.
        """
        if len(packet) < 60:
            padding = b"\x00" * (60 - len(packet))
            packet += padding
        return (_PREAMBLE_SFD + packet +
                struct.pack("<I", zlib.crc32(packet) & 0xFFFFFFFF))

    def idle(self):
        """Helper function to set bus to IDLE state."""
        for i in range(len(self.bus)):
            self.bus[i] = (_XGMII_IDLE, True)
        self.signal.value = self.bus.value

    def terminate(self, index: int) -> None:
        """Helper function to terminate from a provided lane index.

        Args:
            index: The index to terminate.
        """
        self.bus[index] = (_XGMII_TERMINATE, True)

        if index < len(self.bus) - 1:

            for rem in range(index + 1, len(self.bus)):
                self.bus[rem] = (_XGMII_IDLE, True)

    async def _driver_send(self, pkt: bytes, sync: bool = True) -> None:
        """Send a packet over the bus.

        Args:
            pkt: The Ethernet packet to drive onto the bus.
        """
        pkt = self.layer1(bytes(pkt))

        self.log.debug("Sending packet of length %d bytes" % len(pkt))
        self.log.debug(f"Sending Packet:\n{hexdump(pkt, dump=True)}")

        clkedge = RisingEdge(self.clock)
        if sync:
            await clkedge

        self.bus[0] = (_XGMII_START, True)

        for i in range(1, len(self.bus)):
            self.bus[i] = (pkt[i-1], False)

        pkt = pkt[len(self.bus)-1:]
        self.signal.value = self.bus.value
        await clkedge

        done = False

        while pkt:

            for i in range(len(self.bus)):
                if i == len(pkt):
                    self.terminate(i)
                    pkt = b""
                    done = True
                    break
                self.bus[i] = (pkt[i], False)

            self.signal.value = self.bus.value
            await clkedge
            pkt = pkt[len(self.bus):]

        if not done:
            self.terminate(0)
            self.signal.value = self.bus.value
            await clkedge

        self.idle()
        await clkedge
        self.log.debug("Successfully sent packet")
