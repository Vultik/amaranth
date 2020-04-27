from .._utils import deprecated
from .. import *


__all__ = ["FFSynchronizer", "AsyncFFSynchronizer", "ResetSynchronizer", "PulseSynchronizer"]


def _check_stages(stages):
    if not isinstance(stages, int) or stages < 1:
        raise TypeError("Synchronization stage count must be a positive integer, not {!r}"
                        .format(stages))
    if stages < 2:
        raise ValueError("Synchronization stage count may not safely be less than 2")


class FFSynchronizer(Elaboratable):
    """Resynchronise a signal to a different clock domain.

    Consists of a chain of flip-flops. Eliminates metastabilities at the output, but provides
    no other guarantee as to the safe domain-crossing of a signal.

    Parameters
    ----------
    i : Signal(n), in
        Signal to be resynchronised.
    o : Signal(n), out
        Signal connected to synchroniser output.
    o_domain : str
        Name of output clock domain.
    reset : int
        Reset value of the flip-flops. On FPGAs, even if ``reset_less`` is True,
        the :class:`FFSynchronizer` is still set to this value during initialization.
    reset_less : bool
        If ``True`` (the default), this :class:`FFSynchronizer` is unaffected by ``o_domain``
        reset. See "Note on Reset" below.
    stages : int
        Number of synchronization stages between input and output. The lowest safe number is 2,
        with higher numbers reducing MTBF further, at the cost of increased latency.
    max_input_delay : None or float
        Maximum delay from the input signal's clock to the first synchronization stage, in seconds.
        If specified and the platform does not support it, elaboration will fail.

    Platform override
    -----------------
    Define the ``get_ff_sync`` platform method to override the implementation of
    :class:`FFSynchronizer`, e.g. to instantiate library cells directly.

    Note on Reset
    -------------
    :class:`FFSynchronizer` is non-resettable by default. Usually this is the safest option;
    on FPGAs the :class:`FFSynchronizer` will still be initialized to its ``reset`` value when
    the FPGA loads its configuration.

    However, in designs where the value of the :class:`FFSynchronizer` must be valid immediately
    after reset, consider setting ``reset_less`` to False if any of the following is true:

    - You are targeting an ASIC, or an FPGA that does not allow arbitrary initial flip-flop states;
    - Your design features warm (non-power-on) resets of ``o_domain``, so the one-time
      initialization at power on is insufficient;
    - Your design features a sequenced reset, and the :class:`FFSynchronizer` must maintain
      its reset value until ``o_domain`` reset specifically is deasserted.

    :class:`FFSynchronizer` is reset by the ``o_domain`` reset only.
    """
    def __init__(self, i, o, *, o_domain="sync", reset=0, reset_less=True, stages=2,
                 max_input_delay=None):
        _check_stages(stages)

        self.i = i
        self.o = o

        self._reset      = reset
        self._reset_less = reset_less
        self._o_domain   = o_domain
        self._stages     = stages

        self._max_input_delay = max_input_delay

    def elaborate(self, platform):
        if hasattr(platform, "get_ff_sync"):
            return platform.get_ff_sync(self)

        if self._max_input_delay is not None:
            raise NotImplementedError("Platform '{}' does not support constraining input delay "
                                      "for FFSynchronizer"
                                      .format(type(platform).__name__))

        m = Module()
        flops = [Signal(self.i.shape(), name="stage{}".format(index),
                        reset=self._reset, reset_less=self._reset_less)
                 for index in range(self._stages)]
        for i, o in zip((self.i, *flops), flops):
            m.d[self._o_domain] += o.eq(i)
        m.d.comb += self.o.eq(flops[-1])
        return m


class AsyncFFSynchronizer(Elaboratable):
    """Synchronize deassertion of an asynchronous signal.

    The signal driven by the :class:`AsyncFFSynchronizer` is asserted asynchronously and deasserted
    synchronously, eliminating metastability during deassertion.

    This synchronizer is primarily useful for resets and reset-like signals.

    Parameters
    ----------
    i : Signal(1), in
        Asynchronous input signal, to be synchronized.
    o : Signal(1), out
        Synchronously released output signal.
    domain : str
        Name of clock domain to reset.
    stages : int, >=2
        Number of synchronization stages between input and output. The lowest safe number is 2,
        with higher numbers reducing MTBF further, at the cost of increased deassertion latency.
    async_edge : str
        The edge of the input signal which causes the output to be set. Must be one of "pos" or "neg".
    max_input_delay : None or float
        Maximum delay from the input signal's clock to the first synchronization stage, in seconds.
        If specified and the platform does not support it, elaboration will fail.

    Platform override
    -----------------
    Define the ``get_async_ff_sync`` platform method to override the implementation of
    :class:`AsyncFFSynchronizer`, e.g. to instantiate library cells directly.
    """
    def __init__(self, i, o, *, domain="sync", stages=2, async_edge="pos", max_input_delay=None):
        _check_stages(stages)

        self.i = i
        self.o = o

        self._domain = domain
        self._stages = stages

        if async_edge not in ("pos", "neg"):
            raise ValueError("AsyncFFSynchronizer async edge must be one of 'pos' or 'neg', "
                             "not {!r}"
                             .format(async_edge))
        self._edge = async_edge

        self._max_input_delay = max_input_delay

    def elaborate(self, platform):
        if hasattr(platform, "get_async_ff_sync"):
            return platform.get_async_ff_sync(self)

        if self._max_input_delay is not None:
            raise NotImplementedError("Platform '{}' does not support constraining input delay "
                                      "for AsyncFFSynchronizer"
                                      .format(type(platform).__name__))

        m = Module()
        m.domains += ClockDomain("async_ff", async_reset=True, local=True)
        flops = [Signal(1, name="stage{}".format(index), reset=1)
                 for index in range(self._stages)]
        for i, o in zip((0, *flops), flops):
            m.d.async_ff += o.eq(i)

        if self._edge == "pos":
            m.d.comb += ResetSignal("async_ff").eq(self.i)
        else:
            m.d.comb += ResetSignal("async_ff").eq(~self.i)

        m.d.comb += [
            ClockSignal("async_ff").eq(ClockSignal(self._domain)),
            self.o.eq(flops[-1])
        ]

        return m


class ResetSynchronizer(Elaboratable):
    """Synchronize deassertion of a clock domain reset.

    The reset of the clock domain driven by the :class:`ResetSynchronizer` is asserted
    asynchronously and deasserted synchronously, eliminating metastability during deassertion.

    The driven clock domain could use a reset that is asserted either synchronously or
    asynchronously; a reset is always deasserted synchronously. A domain with an asynchronously
    asserted reset is useful if the clock of the domain may be gated, yet the domain still
    needs to be reset promptly; otherwise, synchronously asserted reset (the default) should
    be used.

    Parameters
    ----------
    arst : Signal(1), in
        Asynchronous reset signal, to be synchronized.
    domain : str
        Name of clock domain to reset.
    stages : int, >=2
        Number of synchronization stages between input and output. The lowest safe number is 2,
        with higher numbers reducing MTBF further, at the cost of increased deassertion latency.
    max_input_delay : None or float
        Maximum delay from the input signal's clock to the first synchronization stage, in seconds.
        If specified and the platform does not support it, elaboration will fail.

    Platform override
    -----------------
    Define the ``get_reset_sync`` platform method to override the implementation of
    :class:`ResetSynchronizer`, e.g. to instantiate library cells directly.
    """
    def __init__(self, arst, *, domain="sync", stages=2, max_input_delay=None):
        _check_stages(stages)

        self.arst = arst

        self._domain = domain
        self._stages = stages

        self._max_input_delay = max_input_delay

    def elaborate(self, platform):
        return AsyncFFSynchronizer(self.arst, ResetSignal(self._domain), domain=self._domain,
                stages=self._stages, max_input_delay=self._max_input_delay)


class PulseSynchronizer(Elaboratable):
    """A one-clock pulse on the input produces a one-clock pulse on the output.

    If the output clock is faster than the input clock, then the input may be safely asserted at
    100% duty cycle. Otherwise, if the clock ratio is n : 1, the input may be asserted at most once
    in every n input clocks, else pulses may be dropped.
    Other than this there is no constraint on the ratio of input and output clock frequency.

    Parameters
    ----------
    i_domain : str
        Name of input clock domain.
    o-domain : str
        Name of output clock domain.
    sync_stages : int
        Number of synchronisation flops between the two clock domains. 2 is the default, and
        minimum safe value. High-frequency designs may choose to increase this.
    """
    def __init__(self, i_domain, o_domain, sync_stages=2):
        if not isinstance(sync_stages, int) or sync_stages < 1:
            raise TypeError("sync_stages must be a positive integer, not '{!r}'".format(sync_stages))

        self.i = Signal()
        self.o = Signal()
        self.i_domain = i_domain
        self.o_domain = o_domain
        self.sync_stages = sync_stages

    def elaborate(self, platform):
        m = Module()

        itoggle = Signal()
        otoggle = Signal()
        ff_sync = m.submodules.ff_sync = \
            FFSynchronizer(itoggle, otoggle, o_domain=self.o_domain, stages=self.sync_stages)
        otoggle_prev = Signal()

        m.d[self.i_domain] += itoggle.eq(itoggle ^ self.i)
        m.d[self.o_domain] += otoggle_prev.eq(otoggle)
        m.d.comb += self.o.eq(otoggle ^ otoggle_prev)

        return m
