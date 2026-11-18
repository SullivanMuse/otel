import asyncio
import logging
import traceback
import typing

logger = logging.getLogger(__name__)


COMMAND_RETRY_LIMIT = 3
TIMEOUT = 1
WRITE_EXCEPTIONS = (OSError, BrokenPipeError, ConnectionResetError, TimeoutError)
READ_EXCEPTIONS = (*WRITE_EXCEPTIONS, asyncio.IncompleteReadError, ValueError)
CONNECT_EXCEPTIONS = (*READ_EXCEPTIONS, ConnectionRefusedError)


class SCPIInstrument:
    """SCPI instrument device interface"""

    def __init__(self, name, host, port, no_error=lambda error_response: error_response is None):  # type: ignore[no-untyped-def]
        self.name = name
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
        self.no_error = no_error
        self._logged_connect_error = False

    def is_connected(self) -> bool:
        """is connected"""
        if self.reader is not None:
            assert self.writer is not None
            return True
        return False

    async def connect(self):  # type: ignore[no-untyped-def]
        """connect to instrument; may block indefinitely"""
        while not self.is_connected():
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                await self._clear_error_queue()  # type: ignore[no-untyped-call]
                logger.info("connected: %s", {"host": self.host, "port": self.port})
                self._logged_connect_error = False
                break
            except CONNECT_EXCEPTIONS as exc:
                self.reader = self.writer = None
                if not self._logged_connect_error:
                    logger.error(
                        "error while trying to connect: %s %s %s",
                        type(exc).__qualname__,
                        exc,
                        {"host": self.host, "port": self.port},
                    )
                    logger.debug("traceback: %s", traceback.format_exc())
                    self._logged_connect_error = True

    async def check_error(self):  # type: ignore[no-untyped-def]
        """check if there is an error in the instrument error queue"""
        await self.write("SYST:ERR?")
        error = await self.read()
        if not error:
            return
        error = error.strip()
        if self.no_error(error):
            return
        return error

    async def _clear_error_queue(self):  # type: ignore[no-untyped-def]
        """
        Clear all pre-existing errors from the instrument's error buffer.
        '0, "No Error"' is returned after all other errors have been read
        """
        try:
            logger.debug("Clearing error buffer...")
            error = await self.check_error()  # type: ignore[no-untyped-call]
            logger.info("error: %s", {"error": error, "host": self.host, "port": self.port})
            while not self.no_error(error):
                logger.warning(
                    "Cleared startup error: %s",
                    {"error": error, "name": self.name, "host": self.host, "port": self.port},
                )
                error = await self.check_error()  # type: ignore[no-untyped-call]
            logger.debug("ERROR BUFFER CLEARED")
        except READ_EXCEPTIONS as exc:
            exc.add_note("failed to clear error queue")
            raise

    async def disconnect(self):  # type: ignore[no-untyped-def]
        """disconnect from instrument"""
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.reader = self.writer = None

    async def write(self, message: str):  # type: ignore[no-untyped-def]
        """write message to instrument"""
        await self.connect()  # type: ignore[no-untyped-call]
        assert self.writer is not None
        try:
            self.writer.write((message + "\n").encode())
            await asyncio.wait_for(self.writer.drain(), TIMEOUT)
            logger.debug("write successful: %s", message)
        except WRITE_EXCEPTIONS as exc:
            logger.error("error while writing to socket: %s %s", type(exc).__qualname__, exc)
            self.reader = self.writer = None
            raise

    async def read(self) -> typing.Optional[str]:
        """read message from instrument"""
        await self.connect()  # type: ignore[no-untyped-call]
        assert self.writer is not None
        assert self.reader is not None
        try:
            result = (await asyncio.wait_for(self.reader.readline(), TIMEOUT)).decode()
            logger.debug("read: %s", repr(result))
            if not result.endswith("\n"):
                # incomplete read; socket has been closed by other side
                raise ValueError
            return result  # type: ignore[no-any-return]
        except READ_EXCEPTIONS as exc:
            detail = {
                "host": self.host,
                "port": self.port,
            }
            exc.add_note(f"while reading from socket: {detail}")
            self.reader = self.writer = None
            raise
