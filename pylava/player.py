from inspect import isawaitable
from typing import Optional

from discord import VoiceChannel


# noinspection PyProtectedMember
class Player:
    def __init__(self, connection, guild_id: int):
        self.conn = connection
        self.guild = guild_id
        # dynamic variables:
        self._channel = None
        self._paused = False
        self._playing = False
        self._position = None

        self.track_callback = None  # you can set this one

    @property
    def channel(self) -> Optional[VoiceChannel]:
        """Returns the channel the player is connected to."""
        if self._channel is None:
            return
        return self.conn.bot.get_channel(self._channel)

    @property
    def connected(self) -> bool:
        """Returns the player's connected state."""
        return bool(self.channel)

    @property
    def position(self) -> Optional[float]:
        """Returns the player's current position in seconds."""
        return self._position

    @property
    def paused(self) -> bool:
        """Returns the player's paused state."""
        return self._paused

    @property
    def playing(self) -> bool:
        """Returns the player's playing state."""
        return self._playing

    @property
    def stopped(self) -> bool:
        """Returns the player's stopped (neither playing nor paused) state."""
        return not self.playing and not self.paused

    async def connect(self, channel_id: int):
        """Connects the player to a Discord channel."""
        await self.conn._discord_connect(self.guild, channel_id)
        self._channel = channel_id

    async def disconnect(self):
        """Disconnects the player from Discord."""
        await self.conn._discord_disconnect(self.guild)
        self._channel = None

    async def query(self, *args, **kwargs):
        """Shortcut method for :meth:`Connection.query`."""
        return await self.conn.query(*args, **kwargs)

    async def play(self, track: str):
        """
        Plays a track. If already playing, replaces the current track.

        :param track: A base64 track ID returned by the :meth:`Connection.query` method.
        """
        await self.conn._discord_play(self.guild, track)
        self._playing = True

    async def set_pause(self, paused: bool):
        """Sets the pause state."""
        if paused == self._paused:
            return  # state's already set
        await self.conn._discord_pause(self.guild, paused)
        self._paused = paused

    async def stop(self):
        """Stops the player."""
        await self.conn._discord_stop(self.guild)
        self._playing = False

    async def seek(self, position: float):
        """Seeks to a specific position in a track."""
        await self.conn._discord_seek(self.guild, position)

    async def _process_event(self, data):
        if data['op'] != 'event':
            return

        if data['type'] == 'TrackEndEvent':
            self._playing = False
            self._position = None

            if not self.track_callback:
                return
            if isawaitable(self.track_callback):
                await self.track_callback(self)
            else:
                self.track_callback(self)
