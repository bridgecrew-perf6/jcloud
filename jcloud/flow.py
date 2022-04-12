import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from . import AUTH_HEADERS, LOGSTREAM_API, WOLF_API
from .helper import get_logger, get_or_reuse_loop, get_pbar, upload_project, zipdir

logger = get_logger()

pbar, pb_task = get_pbar('', total=5)


class Status(str, Enum):
    SUBMITTED = 'SUBMITTED'
    NORMALIZING = 'NORMALIZING'
    NORMALIZED = 'NORMALIZED'
    STARTING = 'STARTING'
    FAILED = 'FAILED'
    ALIVE = 'ALIVE'
    UPDATING = 'UPDATING'
    DELETING = 'DELETING'
    DELETED = 'DELETED'

    @property
    def streamable(self) -> bool:
        return self in (Status.ALIVE, Status.UPDATING, Status.DELETING)

    @property
    def alive(self) -> bool:
        return self == Status.ALIVE

    @property
    def deleted(self) -> bool:
        return self == Status.DELETED


@dataclass
class CloudFlow:
    path: Optional[str] = None
    name: Optional[str] = None
    workspace: Optional[str] = None
    flow_id: Optional[str] = None
    artifactid: Optional[str] = None

    @property
    def host(self) -> str:
        return f'{self.name}-{self.flow_id.split("-")[1]}.wolf.jina.ai'

    @property
    def _loop(self):
        return get_or_reuse_loop()

    async def zip_and_upload(self):
        with zipdir(directory=Path(self.path)) as zipfilepath:
            self.artifactid = await upload_project(filepaths=[zipfilepath])

    async def _deploy(self):

        params = {}
        if self.name:
            params['name'] = self.name
        if self.workspace:
            params['workspace'] = self.workspace

        async with aiohttp.ClientSession() as session:
            pbar.update(pb_task, advance=1, description='Submitting...')

            _post_kwargs = dict(url=WOLF_API, headers=AUTH_HEADERS)
            if not Path(self.path).exists():
                logger.error(f'Path {self.path} doesn\'t exist. Cannot deploy the Flow')
            elif Path(self.path).is_dir():
                await self.zip_and_upload()
                params['artifactid'] = self.artifactid
            elif Path(self.path).is_file():
                _post_kwargs['data'] = {'yaml': open(self.path)}

            _post_kwargs['params'] = params
            async with session.post(**_post_kwargs) as response:
                json_response = await response.json()
                print(json_response)
                assert (
                    response.status == HTTPStatus.CREATED
                ), f'Got Invalid response status {response.status}, expected {HTTPStatus.CREATED}'
                if self.name:
                    assert self.name in json_response['name']
                assert Status(json_response['status']) == Status.SUBMITTED

                self.flow_id: str = json_response['id']
                self.workspace: str = json_response['workspace']

                logger.debug(
                    f'POST /flows with flow_id {self.flow_id} & request_id {json_response["request_id"]}'
                )

                self._c_logstream_task = asyncio.create_task(
                    CloudFlow.logstream({'request_id': json_response['request_id']})
                )
                return json_response

    @property
    async def status(self):
        async with aiohttp.ClientSession() as session:
            async with session.get(url=f'{WOLF_API}/{self.flow_id}') as response:
                response.raise_for_status()
                return await response.json()

    async def _fetch_until(
        self,
        intermediate: List[Status],
        desired: Status = Status.ALIVE,
    ):
        wait_seconds = 0
        while wait_seconds < 600:
            json_response = await self.status
            if Status(json_response['status']) == desired:
                gateway = json_response['gateway']
                logger.debug(
                    f'Successfully reached status: {desired} with gateway {gateway}'
                )
                pbar.update(pb_task, description='Live', advance=1)
                return gateway
            else:
                current_status = Status(json_response['status'])
                assert current_status in intermediate
                await asyncio.sleep(1)
                wait_seconds += 1

    async def _terminate(self):
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                url=f'{WOLF_API}/{self.flow_id}',
                headers=AUTH_HEADERS,
            ) as response:
                json_response = await response.json()
                assert (
                    response.status == HTTPStatus.ACCEPTED
                ), f'Got Invalid response status {response.status}, expected {HTTPStatus.ACCEPTED}'
                self.t_logstream_task = asyncio.create_task(
                    CloudFlow.logstream(
                        params={'request_id': json_response['request_id']}
                    )
                )
                assert json_response['id'] == str(self.flow_id)
                assert Status(json_response['status']) == Status.DELETING

    @staticmethod
    async def logstream(params):
        logger.debug(f'Asked to stream logs with params {params}')

        _first_msg = True

        try:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.ws_connect(LOGSTREAM_API, params=params) as ws:
                        logger.debug(
                            f'Successfully connected to logstream API with params: {params}'
                        )
                        await ws.send_json({})
                        async for msg in ws:
                            if msg.type == aiohttp.http.WSMsgType.TEXT:
                                log_dict: Dict = msg.json()
                                if log_dict.get('status') == 'STREAMING':
                                    if _first_msg:
                                        pbar.update(
                                            pb_task,
                                            description='Streaming...',
                                            advance=1,
                                        )
                                        _first_msg = False
                                    logger.debug(log_dict['message'])
                    logger.debug(f'Disconnected from the logstream server ...')
                    pbar.update(pb_task, description='Finished', advance=1)
                except aiohttp.WSServerHandshakeError as e:
                    logger.critical(
                        f'Couldn\'t connect to the logstream server as {e!r}'
                    )
        except asyncio.CancelledError:
            logger.debug(f'logstream task cancelled.')
        except Exception as e:
            logger.error(f'Got an exception while streaming logs {e!r}')

    async def __aenter__(self):
        with pbar:
            pbar.start_task(pb_task)
            await self._deploy()
            pbar.update(pb_task, description='Starting...', advance=1)
            self.gateway = await self._fetch_until(
                intermediate=[
                    Status.SUBMITTED,
                    Status.NORMALIZING,
                    Status.NORMALIZED,
                    Status.STARTING,
                ],
                desired=Status.ALIVE,
            )
            await self._c_logstream_task
            return self

    async def __aexit__(self, *args, **kwargs):
        await self._terminate()
        await self._fetch_until(
            intermediate=[Status.DELETING],
            desired=Status.DELETED,
        )
        # await self.t_logstream_task
        await CloudFlow._cancel_pending()

    @staticmethod
    async def _cancel_pending():
        for task in asyncio.all_tasks():
            task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await task

    def __enter__(self):
        return self._loop.run_until_complete(self.__aenter__())

    def __exit__(self, *args, **kwargs):
        self._loop.run_until_complete(self.__aexit__(*args, **kwargs))
