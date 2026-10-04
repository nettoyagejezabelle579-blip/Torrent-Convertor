"""Exceptions raised by Torrent Convertor."""


class ConvertorError(Exception):
    """Base class for errors that should be shown to the user as a plain message."""


class SourceError(ConvertorError):
    """The .torrent file, magnet link or URL could not be loaded."""


class DownloadError(ConvertorError):
    """The torrent could not be downloaded."""


class ArchiveError(ConvertorError):
    """The zip file could not be created."""


class Cancelled(Exception):
    """The user cancelled the conversion."""
