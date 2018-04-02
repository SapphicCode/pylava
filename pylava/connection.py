import asyncio
import json
import logging
import time
from typing import Union, Optional

import aiohttp
import websockets
from discord.ext import commands

from .errors import Disconnected
from .player import Player

logger = logging.getLogger('pylava')
logger.addHandler(logging.NullHandler())


class Connection:
    def __init__(self, bot: Union[commands.Bot, commands.AutoShardedBot],
                 password: str, ws_url: str, rest_url: str):
        bot.add_listener(self._handler, 'on_socket_response')

        self.bot = bot
        self._loop = bot.loop
        self._autosharded = isinstance(self.bot, commands.AutoShardedBot)
        self._auth = password
        self._url = ws_url
        self._rest_url = rest_url
        self._websocket = None

        self._loop.create_task(self.connect())

        self._players = {}
        self._downed_shards = {}

        self.stats = {}

    async def _handler(self, data):
        if not self.connected:
            return

        if not data:
            return
        if data['op'] != 0:
            return

        if data['t'] == 'VOICE_SERVER_UPDATE':  # voice **SERVER**!!!!!! update! NOT STATE
            payload = {
                'op': 'voiceUpdate',
                'guildId': data['d']['guild_id'],
                'sessionId': self.bot.get_guild(int(data['d']['guild_id'])).me.voice.session_id,
                'event': data['d']
            }
            await self._send(**payload)

    async def _discord_connection_state_loop(self):
        while self.connected:
            shard_guild_map = {}
            for guild in self._players:
                if not self._players[guild].connected:
                    continue

                guild_shard_id = (guild >> 22) % self.bot.shard_count if self.bot.shard_count is not None else 1

                if guild_shard_id not in shard_guild_map:
                    shard_guild_map[guild_shard_id] = []
                shard_guild_map[guild_shard_id].append(guild)

            for shard, guilds in shard_guild_map.items():
                ws = self._get_discord_ws(shard)
                if not ws or not ws.open:
                    self._downed_shards[shard] = True
                    logger.debug('Shard {} detected as offline, handling...'.format(shard))
                    continue

                if self._downed_shards.get(shard):
                    self._downed_shards.pop(shard)
                    self._loop.create_task(self._discord_reconnect_task(guilds))
                    logger.debug('Shard {} detected as online again, reconnecting guilds...'.format(shard))

            await asyncio.sleep(0.1)

    async def _discord_reconnect_task(self, guilds):
        await asyncio.sleep(10)  # fixed wait for READY / RESUMED
        for guild in guilds:
            # noinspection PyProtectedMember
            await self._players[guild].connect(self._players[guild]._channel)
            await asyncio.sleep(1)  # 1 connection / second (gateway ratelimits = bad)

    async def _lava_event_processor(self):
        while self.connected:
            try:
                data = json.loads(await self._websocket.recv())
            except (websockets.ConnectionClosed, AttributeError):
                return  # oh well

            logger.debug('Received a payload from Lavalink: {}'.format(data))
            op = data.get('op')

            if op == 'stats':
                data.pop('op')
                self.stats = data

            if op == 'playerUpdate' and 'position' in data['state']:
                player = self.get_player(int(data['guildId']))

                lag = time.time() - data['state']['time'] / 1000
                player._position = data['state']['position'] / 1000 + lag
            if op == 'event':
                player = self.get_player(int(data['guildId']))
                # noinspection PyProtectedMember
                self._loop.create_task(player._process_event(data))

    def _get_discord_ws(self, shard_id):
        if self._autosharded:
            return self.bot.shards[shard_id].ws
        if self.bot.shard_id is None or self.bot.shard_id == shard_id:
            # only return if the shard actually matches the current shard, useful for ignoring events not meant for us
            return self.bot.ws

    async def connect(self):
        """Connects to Lavalink. Gets automatically called once when the Connection object is created."""
        await self.bot.wait_until_ready()
        headers = {
            'Authorization': self._auth,
            'Num-Shards': self.bot.shard_count if self.bot.shard_count is not None else 1,
            'User-Id': self.bot.user.id
        }
        self._websocket = await websockets.connect(self._url, extra_headers=headers)
        self._loop.create_task(self._lava_event_processor())
        self._loop.create_task(self._discord_connection_state_loop())
        logger.info('Successfully connected to Lavalink.')

    async def query(self, query: str, *, retry_count=0, retry_delay=0) -> list:
        """
        Queries Lavalink. Returns a list of Track objects (dictionaries).

        :param query: The search query to make.
        :param retry_count: How often to retry the query should it fail. 0 disables, -1 will try forever (dangerous).
        :param retry_delay: How long to sleep for between retries.
        """
        headers = {
            'Authorization': self._auth,
            'Accept': 'application/json'
        }
        params = {
            'identifier': query
        }
        async with aiohttp.ClientSession(headers=headers) as s:
            while True:
                async with s.get(self._rest_url+'/loadtracks', params=params) as resp:
                    out = await resp.json()

                # -1 is not recommended unless you run it as a task which you cancel after a specific time, but
                # you do you devs
                if not out and (retry_count > 0 or retry_count < 0):  # edge case where lavalink just returns nothing
                    retry_count -= 1
                    if retry_delay:
                        await asyncio.sleep(retry_delay)
                else:
                    break
        return out

    @property
    def connected(self) -> bool:
        """Returns the Lavalink connection state."""
        if self._websocket is None:
            return False
        else:
            return self._websocket.open

    async def disconnect(self):
        """Disconnects from Lavalink."""
        if not self.connected:
            raise Disconnected()
        await self._websocket.close()
        self._websocket = None
        self._players = {}  # this is why you shouldn't hold references to players for too long
        logger.info('Disconnected from Lavalink and reset state.')

    async def wait_until_ready(self):
        """Waits indefinitely until the Lavalink connection has been established."""
        while not self.connected:
            await asyncio.sleep(0.01)

    async def _send(self, **data):
        if not self.connected:
            logger.debug('Refusing to send a payload to Lavalink due to websocket disconnection.')
            raise Disconnected()  # refuse to send anything

        if isinstance(data, dict):
            if isinstance(data.get('guildId'), int):
                data['guildId'] = str(data['guildId'])
            if isinstance(data.get('channelId'), int):
                data['channelId'] = str(data['channelId'])
        logger.debug('Sending a payload to Lavalink: {}'.format(data))
        await self._websocket.send(json.dumps(data))

    async def _discord_disconnect(self, guild_id: int):
        if self._autosharded:
            shard_id = (guild_id >> 22) % self.bot.shard_count
        else:
            shard_id = self.bot.shard_id
        await self._get_discord_ws(shard_id).send(json.dumps({
            'op': 4,
            'd': {
                'self_deaf': False,
                'guild_id': str(guild_id),
                'channel_id': None,
                'self_mute': False
            }
        }))

    async def _discord_connect(self, guild_id: int, channel_id: int):
        if self._autosharded:
            shard_id = (guild_id >> 22) % self.bot.shard_count
        else:
            shard_id = self.bot.shard_id
        await self._get_discord_ws(shard_id).send(json.dumps({
            'op': 4,
            'd': {
                'self_deaf': False,
                'guild_id': str(guild_id),
                'channel_id': str(channel_id),
                'self_mute': False
            }
        }))

    async def _discord_play(self, guild_id: int, track: str, start_time: float, end_time: Optional[float]):
        if end_time is not None:
            await self._send(op='play', guildId=guild_id, track=track,
                             startTime=int(start_time*1000), endTime=int(end_time*1000))
        else:
            await self._send(op='play', guildId=guild_id, track=track, startTime=int(start_time*1000))

    async def _discord_pause(self, guild_id: int, paused: bool):
        await self._send(op='pause', guildId=guild_id, pause=paused)

    async def _discord_stop(self, guild_id: int):
        await self._send(op='stop', guildId=guild_id)

    async def _discord_volume(self, guild_id: int, level: int):
        level = max(min(level, 150), 0)
        await self._send(op='volume', guildId=guild_id, volume=level)
        return level

    async def _discord_seek(self, guild_id: int, position: float):
        position = int(position * 1000)
        await self._send(op='seek', guildId=guild_id, position=position)

    def get_player(self, guild_id: int) -> Player:
        """
        Gets a Player class that abstracts away connection handling, among other things.

        You shouldn't be holding onto these for too long, rather you should be requesting them as necessary using this
        method.

        :param guild_id: The guild ID to get the player for.
        :return: A Player instance for that guild.
        """
        player = self._players.get(guild_id)
        if player is None:
            player = Player(self, guild_id)
            self._players[guild_id] = player
        return player
