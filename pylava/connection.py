import asyncio
import json
import time
from typing import Union

import aiohttp
import websockets
from discord.ext import commands

from .errors import Disconnected
from .player import Player


class Connection:
    _websocket = None

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

        self.stats = {}

    async def _handler(self, data):
        if not self.connected:
            return

        if not data:
            return
        if data['op'] != 0:
            return

        if data['t'] != 'VOICE_SERVER_UPDATE':  # voice **SERVER**!!!!!! update! NOT STATE
            return

        payload = {
            'op': 'voiceUpdate',
            'guildId': data['d']['guild_id'],
            'sessionId': self.bot.get_guild(int(data['d']['guild_id'])).me.voice.session_id,
            'event': data['d']
        }
        await self._send(**payload)

    async def _lava_event_processor(self):
        while self.connected:
            try:
                data = json.loads(await self._websocket.recv())
            except websockets.ConnectionClosed or AttributeError:
                return  # oh well

            op = data.get('op')

            if op == 'validationReq':
                self._loop.create_task(self._lava_validation_request(data))
            if op == 'isConnectedReq':
                self._loop.create_task(self._lava_connection_request(data))
            if op == 'sendWS':
                self._loop.create_task(self._lava_forward_ws(data))

            if op == 'stats':
                data.pop('op')
                self.stats = data

            if op == 'playerUpdate':
                player = self.get_player(int(data['guildId']))

                lag = time.time() - data['state']['time'] / 1000
                player._position = data['state']['position'] / 1000 + lag
                player._paused = False
            if op == 'event':
                player = self.get_player(int(data['guildId']))
                # noinspection PyProtectedMember
                self._loop.create_task(player._process_event(data))

    async def _discord_connection_handler(self):
        async def panic(shard_id):
            for guild in tuple(self._players):
                if (guild >> 22) % self.bot.shard_count != shard_id:
                    continue

                player = self._players[guild]
                if player is not None:
                    await player.disconnect()
                
                del self._players[guild]

        while self.connected:
            shards_to_check = set([(x >> 22) % self.bot.shard_count for x in self._players])
            for shard in shards_to_check:
                ws = self._get_discord_ws(shard)
                if ws is not None and ws.open:
                    continue
                await panic(shard)
            await asyncio.sleep(0.1)

    async def _lava_validation_request(self, data):
        guild = self.bot.get_guild(int(data['guildId']))
        channel = data.get('channelId')

        # guild-only check
        if channel is None:
            if guild is None:
                return await self._send(op='validationRes', guildId=data['guildId'], valid=False)
            return await self._send(op='validationRes', guildId=data['guildId'], valid=True)

        # guild with channel and access check
        channel = self.bot.get_channel(int(channel))
        if channel is None:
            return await self._send(op='validationRes', guildId=data['guildId'], channelId=data['channelId'],
                                    valid=False)
        if not guild.me.permissions_in(channel).connect:
            return await self._send(op='validationRes', guildId=data['guildId'], channelId=data['channelId'],
                                    valid=False)
        await self._send(op='validationRes', guildId=data['guildId'], channelId=data['channelId'], valid=True)

    def _get_discord_ws(self, shard_id):
        if self._autosharded:
            return self.bot.shards[shard_id].ws
        if self.bot.shard_id is None or self.bot.shard_id == shard_id:
            # only return if the shard actually matches the current shard, useful for ignoring events not meant for us
            return self.bot.ws

    async def _lava_connection_request(self, data):
        # autosharded clients
        ws = self._get_discord_ws(data['shardId'])
        if ws is None:
            return await self._send(op='isConnectedRes', shardId=data['shardId'], connected=False)
        return await self._send(op='isConnectedRes', shardId=data['shardId'], connected=ws.open)

    async def _lava_forward_ws(self, data):
        ws = self._get_discord_ws(data['shardId'])
        if ws is None:
            return
        await ws.send(data['message'])

    async def connect(self):
        """Connects to Lavalink."""
        await self.bot.wait_until_ready()
        headers = {
            'Authorization': self._auth,
            'Num-Shards': self.bot.shard_count,
            'User-Id': self.bot.user.id
        }
        self._websocket = await websockets.connect(self._url, extra_headers=headers)
        self._loop.create_task(self._lava_event_processor())
        self._loop.create_task(self._discord_connection_handler())

    async def query(self, query):
        """Query Lavalink."""
        headers = {
            'Authorization': self._auth,
            'Accept': 'application/json'
        }
        params = {
            'identifier': query
        }
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(self._rest_url+'/loadtracks', params=params) as resp:
                return await resp.json()

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

    async def wait_until_ready(self):
        """Waits indefinitely until the lavalink connection has been established."""
        while not self.connected:
            await asyncio.sleep(0.01)

    async def _send(self, **data):
        if not self.connected:
            raise Disconnected()  # refuse to send anything

        if data.get('validationRes') and data.get('valid') is False:  # player connection failure handling
            self.get_player(int(data['guildId']))._channel = None

        if isinstance(data, dict):
            if isinstance(data.get('guildId'), int):
                data['guildId'] = str(data['guildId'])
            if isinstance(data.get('channelId'), int):
                data['channelId'] = str(data['channelId'])
        await self._websocket.send(json.dumps(data))

    async def _discord_connect(self, guild_id: int, channel_id: int):
        await self._send(op='connect', guildId=guild_id, channelId=channel_id)

    async def _discord_disconnect(self, guild_id: int):
        # sending the disconnect to Discord
        # count = self.bot.shard_count if not self._autosharded else len(self.bot.shards)
        # ws = self._get_discord_ws((guild_id >> 22) % count)
        # disconnect_packet = {
        #     'op': 4,
        #     'd': {
        #         'self_deaf': False,
        #         'guild_id': str(guild_id),
        #         'channel_id': None,
        #         'self_mute': False
        #     }
        # }
        # await ws.send(json.dumps(disconnect_packet))

        # sending the disconnect to lavalink (which *should* handle this but doesn't)
        await self._send(op='disconnect', guildId=guild_id)

    async def _discord_play(self, guild_id: int, track: str):
        await self._send(op='play', guildId=guild_id, track=track)

    async def _discord_pause(self, guild_id: int, paused: bool):
        await self._send(op='pause', guildId=guild_id, pause=paused)

    async def _discord_stop(self, guild_id: int):
        await self._send(op='stop', guildId=guild_id)

    async def _discord_volume(self, guild_id: int, level: int):
        level = max(min(level, 150), 0)
        await self._send(op='volume', guildId=guild_id, volume=level)

    async def _discord_seek(self, guild_id: int, position: float):
        position = int(position * 1000)
        await self._send(op='seek', guildId=guild_id, position=position)

    def get_player(self, guild_id) -> Player:
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
