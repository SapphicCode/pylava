import asyncio
import json
import logging
import time
from typing import Union

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
        self._limbo = {}
        self._unlimbo_tasks = {}

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

            logger.debug('Received a payload from Lavalink: {}'.format(data))
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

            if op == 'playerUpdate' and 'position' in data['state']:
                player = self.get_player(int(data['guildId']))

                lag = time.time() - data['state']['time'] / 1000
                player._position = data['state']['position'] / 1000 + lag
            if op == 'event':
                player = self.get_player(int(data['guildId']))
                # noinspection PyProtectedMember
                self._loop.create_task(player._process_event(data))

    # noinspection PyProtectedMember
    async def _unlimbo_shard(self, shard_id):
        op_unpause = 0

        while True:
            await asyncio.sleep(0.1)
            ws = self._get_discord_ws(shard_id)
            if ws is None:
                continue
            if not ws.open:
                continue
            await ws.wait_for('READY', lambda x: True)
            await asyncio.sleep(5)  # GUILD_CREATE events, etc
            break

        for guild in tuple(self._limbo):
            sid = (guild >> 22) % self.bot.shard_count
            if sid != shard_id:
                continue

            actions = self._limbo.pop(guild)
            player = self._players.get(guild)
            if player is None:
                continue  # oh well

            await player.connect(actions[0])
            if op_unpause in actions:
                await player.set_pause(False)

        del self._unlimbo_tasks[shard_id]

    # noinspection PyProtectedMember
    async def _discord_connection_handler(self):
        def panicking(shard_id):
            ws = self._get_discord_ws(shard_id)
            if ws is not None and ws.open:
                return False
            return True

        op_unpause = 0

        while self.connected:
            await asyncio.sleep(0.1)

            shards_to_check = {(x >> 22) % self.bot.shard_count for x in self._players}
            shards_panicking = [x for x in shards_to_check if panicking(x)]

            # pause relevant guilds
            if shards_panicking:
                for guild in tuple(self._players):
                    if guild in self._limbo:  # we're aware of the issue, ignore
                        continue

                    sid = (guild >> 22) % self.bot.shard_count

                    if sid not in shards_panicking:
                        continue

                    player = self._players[guild]
                    if not player._channel:
                        continue

                    ch = player._channel
                    await player.disconnect()

                    actions = [ch]
                    if not player.paused:
                        await player.set_pause(True)
                        actions.append(op_unpause)
                    self._limbo[guild] = actions

                    if sid not in self._unlimbo_tasks:
                        self._unlimbo_tasks[sid] = self._loop.create_task(self._unlimbo_shard(sid))

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
        if not ws.open:
            return
        await ws.send(data['message'])

    async def connect(self):
        """Connects to Lavalink. Gets automatically called once when the Connection object is created."""
        await self.bot.wait_until_ready()
        headers = {
            'Authorization': self._auth,
            'Num-Shards': self.bot.shard_count,
            'User-Id': self.bot.user.id
        }
        self._websocket = await websockets.connect(self._url, extra_headers=headers)
        self._loop.create_task(self._lava_event_processor())
        self._loop.create_task(self._discord_connection_handler())
        logger.info('Successfully connected to Lavalink.')

    async def query(self, query: str) -> list:
        """
        Queries Lavalink. Returns a list of Track objects.

        :param query: The search query to make.
        """
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
        logger.info('Disconnected from Lavalink and reset state.')

    async def wait_until_ready(self):
        """Waits indefinitely until the Lavalink connection has been established."""
        while not self.connected:
            await asyncio.sleep(0.01)

    async def _send(self, **data):
        if not self.connected:
            logger.debug('Refusing to send a payload to Lavalink due to websocket disconnection.')
            raise Disconnected()  # refuse to send anything

        if data.get('op') == 'validationRes' and data.get('valid') is False:  # player connection failure handling
            self.get_player(int(data['guildId']))._channel = None

        if isinstance(data, dict):
            if isinstance(data.get('guildId'), int):
                data['guildId'] = str(data['guildId'])
            if isinstance(data.get('channelId'), int):
                data['channelId'] = str(data['channelId'])
        logger.debug('Sending a payload to Lavalink: {}'.format(data))
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
