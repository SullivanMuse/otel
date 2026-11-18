"""Simulated single-channel DC power supply with a SCPI command interface.

The supply drives a simulated resistive load. With the output on, it regulates
in constant-voltage (CV) mode until the load would draw more than the current
limit, at which point it switches to constant-current (CC) mode. Measurements
add Gaussian noise to the ideal values.

Supported commands (long or short form, case-insensitive; [] = optional node):

    *IDN?  *RST  *CLS  *OPC  *OPC?  *WAI  *TST?  *ESR?
    SYSTem:ERRor[:NEXT]?
    SYSTem:VERSion?
    [SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude] <V>|MIN|MAX|DEF|UP|DOWN
    [SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]? [MIN|MAX|DEF]
    [SOURce:]VOLTage:PROTection[:LEVel] <V>|MIN|MAX|DEF
    [SOURce:]VOLTage:PROTection[:LEVel]? [MIN|MAX|DEF]
    [SOURce:]VOLTage:PROTection:TRIPped?
    [SOURce:]VOLTage:PROTection:CLEar
    [SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude] <A>|MIN|MAX|DEF|UP|DOWN
    [SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]? [MIN|MAX|DEF]
    APPLy <V>[,<A>]
    APPLy?
    OUTPut[:STATe] ON|OFF|1|0
    OUTPut[:STATe]?
    OUTPut:MODE?                         -> CV | CC | OFF | OVP
    MEASure[:SCALar]:VOLTage[:DC]?
    MEASure[:SCALar]:CURRent[:DC]?
    MEASure[:SCALar]:POWer[:DC]?
    FETCh[:SCALar]:VOLTage|CURRent|POWer[:DC]?
    SIMulation:LOAD[:RESistance] <ohms>  (simulator only: set load resistance)
    SIMulation:LOAD[:RESistance]?

Multiple commands may be sent on one line separated by ';'. Responses to all
queries on a line are joined with ';' and returned as a single line. Commands
that are not queries produce no response; errors go to the error queue.
"""

from __future__ import annotations

import dataclasses
import random
import re
from collections import deque
from typing import Callable


class SCPIError(Exception):
    def __init__(self, code: int, message: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        text = self.message if self.detail is None else f"{self.message}; {self.detail}"
        return f'{self.code},"{text}"'


def command_error(detail=None):
    return SCPIError(-100, "Command error", detail)


def syntax_error(detail=None):
    return SCPIError(-102, "Syntax error", detail)


def data_type_error(detail=None):
    return SCPIError(-104, "Data type error", detail)


def parameter_not_allowed(detail=None):
    return SCPIError(-108, "Parameter not allowed", detail)


def missing_parameter(detail=None):
    return SCPIError(-109, "Missing parameter", detail)


def undefined_header(detail=None):
    return SCPIError(-113, "Undefined header", detail)


def out_of_range(detail=None):
    return SCPIError(-222, "Data out of range", detail)


def illegal_value(detail=None):
    return SCPIError(-224, "Illegal parameter value", detail)


QUEUE_OVERFLOW = SCPIError(-350, "Queue overflow")
NO_ERROR = '0,"No error"'


# --------------------------------------------------------------------------
# Header pattern matching
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Node:
    short: str
    long: str
    optional: bool

    def matches(self, token: str) -> bool:
        token = token.upper()
        return token == self.short or token == self.long


def _compile_pattern(pattern: str) -> list[_Node]:
    """'[SOURce]:VOLTage[:LEVel]' -> nodes; the uppercase prefix is the short form."""
    nodes = []
    for match in re.finditer(r"(\[)?:?([A-Za-z*]+\??)(\])?", pattern):
        name = match.group(2).rstrip("?")
        short = "".join(c for c in name if c.isupper() or c == "*")
        nodes.append(_Node(short=short, long=name.upper(), optional=bool(match.group(1))))
    return nodes


def _match_nodes(nodes: list[_Node], tokens: list[str]) -> bool:
    if not nodes:
        return not tokens
    node, rest = nodes[0], nodes[1:]
    if tokens and node.matches(tokens[0]) and _match_nodes(rest, tokens[1:]):
        return True
    return node.optional and _match_nodes(rest, tokens)


# --------------------------------------------------------------------------
# Parameter parsing
# --------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z]*)\s*$"
)
_MULTIPLIERS = {"": 1.0, "U": 1e-6, "M": 1e-3, "K": 1e3}  # SCPI suffixes: M = milli
_UNITS = ("V", "A", "W", "OHM")


def _parse_number(text: str) -> float:
    match = _NUMBER_RE.match(text)
    if not match:
        raise data_type_error(f"expected number, got {text.strip()!r}")
    value = float(match.group(1))
    suffix = match.group(2).upper()
    if not suffix:
        return value
    for unit in _UNITS:
        if suffix.endswith(unit):
            prefix = suffix[: -len(unit)]
            if prefix in _MULTIPLIERS:
                return value * _MULTIPLIERS[prefix]
    raise SCPIError(-131, "Invalid suffix", suffix)


def _parse_bool(text: str) -> bool:
    word = text.strip().upper()
    if word in ("ON", "1"):
        return True
    if word in ("OFF", "0"):
        return False
    raise illegal_value(f"expected ON|OFF|1|0, got {text.strip()!r}")


def _split_params(text: str) -> list[str]:
    text = text.strip()
    return [p.strip() for p in text.split(",")] if text else []


def _fmt(value: float) -> str:
    return f"{value:.6f}"


# --------------------------------------------------------------------------
# Instrument model
# --------------------------------------------------------------------------


@dataclasses.dataclass
class SupplyLimits:
    max_voltage: float = 30.0
    max_current: float = 5.0
    max_ovp: float = 33.0
    voltage_step: float = 0.1
    current_step: float = 0.01


@dataclasses.dataclass
class NoiseModel:
    """Gaussian noise: sigma = gain * |reading| + offset."""

    voltage_gain: float = 0.0002
    voltage_offset: float = 0.0005
    current_gain: float = 0.0005
    current_offset: float = 0.0002

    def voltage(self, rng: random.Random, value: float) -> float:
        return value + rng.gauss(0.0, self.voltage_gain * abs(value) + self.voltage_offset)

    def current(self, rng: random.Random, value: float) -> float:
        return value + rng.gauss(0.0, self.current_gain * abs(value) + self.current_offset)


class PowerSupply:
    IDN = "OTEL-SIM,PSU-3005,SIM000001,1.0.0"
    ERROR_QUEUE_SIZE = 20

    def __init__(
        self,
        limits: SupplyLimits | None = None,
        noise: NoiseModel | None = None,
        load_resistance: float = 10.0,
        seed: int | None = None,
    ):
        self.limits = limits or SupplyLimits()
        self.noise = noise or NoiseModel()
        self.load_resistance = load_resistance
        self.rng = random.Random(seed)
        self.errors: deque[SCPIError] = deque()
        self.esr = 0
        self._commands = self._build_command_table()
        self.reset()

    # ----- state ---------------------------------------------------------

    def reset(self) -> None:
        self.voltage_setpoint = 0.0
        self.current_limit = 1.0
        self.ovp_level = self.limits.max_ovp
        self.ovp_tripped = False
        self.output_enabled = False

    def _ideal_output(self) -> tuple[float, float, str]:
        """Return (voltage, current, mode) at the output terminals."""
        if self.ovp_tripped:
            return 0.0, 0.0, "OVP"
        if not self.output_enabled:
            return 0.0, 0.0, "OFF"
        r = self.load_resistance
        if r <= 0:  # dead short
            return 0.0, self.current_limit, "CC"
        current = self.voltage_setpoint / r
        if current <= self.current_limit:
            return self.voltage_setpoint, current, "CV"
        return self.current_limit * r, self.current_limit, "CC"

    def _check_ovp(self) -> None:
        if self.output_enabled and self.voltage_setpoint > self.ovp_level:
            self.ovp_tripped = True
            self.output_enabled = False

    def measure_voltage(self) -> float:
        voltage, _, _ = self._ideal_output()
        return self.noise.voltage(self.rng, voltage)

    def measure_current(self) -> float:
        _, current, _ = self._ideal_output()
        return self.noise.current(self.rng, current)

    def measure_power(self) -> float:
        return self.measure_voltage() * self.measure_current()

    # ----- error queue ---------------------------------------------------

    def push_error(self, error: SCPIError) -> None:
        if len(self.errors) >= self.ERROR_QUEUE_SIZE:
            self.errors[-1] = QUEUE_OVERFLOW
            return
        self.errors.append(error)
        # set the matching standard event status register bit
        if -199 <= error.code <= -100:
            self.esr |= 1 << 5  # command error
        elif -299 <= error.code <= -200:
            self.esr |= 1 << 4  # execution error
        elif -399 <= error.code <= -300:
            self.esr |= 1 << 3  # device-dependent error

    def pop_error(self) -> str:
        return str(self.errors.popleft()) if self.errors else NO_ERROR

    # ----- SCPI dispatch -------------------------------------------------

    def handle_line(self, line: str) -> str | None:
        """Process one line of SCPI input; return the response line or None."""
        responses: list[str] = []
        prefix: list[str] = []  # current path for ';'-chained commands
        for raw in line.strip().split(";"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                response, prefix = self._execute(raw, prefix)
            except SCPIError as error:
                self.push_error(error)
                continue
            if response is not None:
                responses.append(response)
        return ";".join(responses) if responses else None

    def _execute(self, raw: str, prefix: list[str]) -> tuple[str | None, list[str]]:
        header, _, params = raw.partition(" ")
        is_query = header.endswith("?")
        header = header.rstrip("?")
        if not header:
            raise syntax_error(raw)

        if header.startswith("*"):
            candidates = [[header]]
        elif header.startswith(":"):
            candidates = [header[1:].split(":")]
        else:
            tokens = header.split(":")
            # relative to previous command's path first, then from the root
            candidates = [prefix + tokens, tokens] if prefix else [tokens]

        for tokens in candidates:
            if any(not t for t in tokens):
                raise syntax_error(raw)
            handler = self._lookup(tokens, is_query)
            if handler is not None:
                new_prefix = prefix if tokens[0].startswith("*") else tokens[:-1]
                return handler(_split_params(params)), new_prefix
        raise undefined_header(header + ("?" if is_query else ""))

    def _lookup(self, tokens: list[str], is_query: bool) -> Callable | None:
        for nodes, query, handler in self._commands:
            if query == is_query and _match_nodes(nodes, tokens):
                return handler
        return None

    def _build_command_table(self) -> list[tuple[list[_Node], bool, Callable]]:
        volt = "[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]"
        curr = "[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]"
        ovp = "[SOURce]:VOLTage:PROTection[:LEVel]"
        table = [
            ("*IDN", True, self._idn),
            ("*RST", False, self._rst),
            ("*CLS", False, self._cls),
            ("*OPC", False, self._no_params(lambda: None)),
            ("*OPC", True, self._no_params(lambda: "1")),
            ("*WAI", False, self._no_params(lambda: None)),
            ("*TST", True, self._no_params(lambda: "0")),
            ("*ESR", True, self._esr),
            ("SYSTem:ERRor[:NEXT]", True, self._no_params(self.pop_error)),
            ("SYSTem:VERSion", True, self._no_params(lambda: "1999.0")),
            (ovp, False, self._set_ovp),
            (ovp, True, self._query_ovp),
            ("[SOURce]:VOLTage:PROTection:TRIPped", True,
             self._no_params(lambda: "1" if self.ovp_tripped else "0")),
            ("[SOURce]:VOLTage:PROTection:CLEar", False, self._no_params(self._clear_ovp)),
            (volt, False, self._set_voltage),
            (volt, True, self._query_voltage),
            (curr, False, self._set_current),
            (curr, True, self._query_current),
            ("APPLy", False, self._apply),
            ("APPLy", True,
             self._no_params(lambda: f"{_fmt(self.voltage_setpoint)},{_fmt(self.current_limit)}")),
            ("OUTPut[:STATe]", False, self._set_output),
            ("OUTPut[:STATe]", True,
             self._no_params(lambda: "1" if self.output_enabled else "0")),
            ("OUTPut:MODE", True, self._no_params(lambda: self._ideal_output()[2])),
            ("SIMulation:LOAD[:RESistance]", False, self._set_load),
            ("SIMulation:LOAD[:RESistance]", True,
             self._no_params(lambda: _fmt(self.load_resistance))),
        ]
        for root in ("MEASure", "FETCh"):
            table += [
                (f"{root}[:SCALar]:VOLTage[:DC]", True,
                 self._no_params(lambda: _fmt(self.measure_voltage()))),
                (f"{root}[:SCALar]:CURRent[:DC]", True,
                 self._no_params(lambda: _fmt(self.measure_current()))),
                (f"{root}[:SCALar]:POWer[:DC]", True,
                 self._no_params(lambda: _fmt(self.measure_power()))),
            ]
        return [(_compile_pattern(pattern), query, handler) for pattern, query, handler in table]

    # ----- handlers ------------------------------------------------------

    @staticmethod
    def _no_params(fn: Callable[[], str | None]) -> Callable[[list[str]], str | None]:
        def handler(params: list[str]) -> str | None:
            if params:
                raise parameter_not_allowed()
            return fn()

        return handler

    @staticmethod
    def _one_param(params: list[str]) -> str:
        if not params:
            raise missing_parameter()
        if len(params) > 1:
            raise parameter_not_allowed()
        return params[0]

    def _idn(self, params):
        if params:
            raise parameter_not_allowed()
        return self.IDN

    def _rst(self, params):
        if params:
            raise parameter_not_allowed()
        self.reset()

    def _cls(self, params):
        if params:
            raise parameter_not_allowed()
        self.errors.clear()
        self.esr = 0

    def _esr(self, params):
        if params:
            raise parameter_not_allowed()
        value, self.esr = self.esr, 0
        return str(value)

    def _resolve_level(self, text: str, current: float, maximum: float,
                       default: float, step: float) -> float:
        word = text.strip().upper()
        if word in ("MIN", "MINIMUM"):
            return 0.0
        if word in ("MAX", "MAXIMUM"):
            return maximum
        if word in ("DEF", "DEFAULT"):
            return default
        if word == "UP":
            value = current + step
        elif word == "DOWN":
            value = current - step
        else:
            value = _parse_number(text)
        if not 0.0 <= value <= maximum:
            raise out_of_range(f"{value:g} not in [0, {maximum:g}]")
        return value

    def _query_level(self, params, current: float, maximum: float, default: float) -> str:
        if not params:
            return _fmt(current)
        word = self._one_param(params).upper()
        if word in ("MIN", "MINIMUM"):
            return _fmt(0.0)
        if word in ("MAX", "MAXIMUM"):
            return _fmt(maximum)
        if word in ("DEF", "DEFAULT"):
            return _fmt(default)
        raise illegal_value(word)

    def _set_voltage(self, params):
        self.voltage_setpoint = self._resolve_level(
            self._one_param(params), self.voltage_setpoint,
            self.limits.max_voltage, 0.0, self.limits.voltage_step,
        )
        self._check_ovp()

    def _query_voltage(self, params):
        return self._query_level(params, self.voltage_setpoint, self.limits.max_voltage, 0.0)

    def _set_current(self, params):
        self.current_limit = self._resolve_level(
            self._one_param(params), self.current_limit,
            self.limits.max_current, 1.0, self.limits.current_step,
        )

    def _query_current(self, params):
        return self._query_level(params, self.current_limit, self.limits.max_current, 1.0)

    def _set_ovp(self, params):
        self.ovp_level = self._resolve_level(
            self._one_param(params), self.ovp_level,
            self.limits.max_ovp, self.limits.max_ovp, self.limits.voltage_step,
        )
        self._check_ovp()

    def _query_ovp(self, params):
        return self._query_level(params, self.ovp_level, self.limits.max_ovp, self.limits.max_ovp)

    def _clear_ovp(self):
        if self.voltage_setpoint > self.ovp_level:
            raise SCPIError(-221, "Settings conflict", "voltage setpoint exceeds OVP level")
        self.ovp_tripped = False

    def _apply(self, params):
        if not params:
            raise missing_parameter()
        if len(params) > 2:
            raise parameter_not_allowed()
        voltage = self._resolve_level(params[0], self.voltage_setpoint,
                                      self.limits.max_voltage, 0.0, self.limits.voltage_step)
        current = self.current_limit
        if len(params) == 2:
            current = self._resolve_level(params[1], self.current_limit,
                                          self.limits.max_current, 1.0, self.limits.current_step)
        self.voltage_setpoint, self.current_limit = voltage, current
        self._check_ovp()

    def _set_output(self, params):
        enable = _parse_bool(self._one_param(params))
        if enable and self.ovp_tripped:
            raise SCPIError(-221, "Settings conflict", "OVP tripped; send VOLT:PROT:CLE")
        self.output_enabled = enable
        self._check_ovp()

    def _set_load(self, params):
        value = _parse_number(self._one_param(params))
        if value < 0:
            raise out_of_range("load resistance must be >= 0")
        self.load_resistance = value
