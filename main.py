import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import aiohttp
import cachetools
import discord
import sentry_sdk
from fastapi import FastAPI, Request

from config import SENTRY_DSN

sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.session = aiohttp.ClientSession()
    yield
    await app.session.close()


app = FastAPI(title="BGAW", docs_url=None, redoc_url=None, lifespan=lifespan)
cache = cachetools.TTLCache(maxsize=100, ttl=10 * 60)  # commitsha: WorkflowEmbed


class WorkflowEmbed:
    def __init__(self, event_name: str, payload: Dict[str, Any]) -> None:
        self.embed = discord.Embed(color=discord.Color.og_blurple())
        self.workflows: Dict[int, Dict[str, str]] = {} # workflow_id: {color, name}
        self.message: Optional[discord.WebhookMessage] = None
        self.create_embed(payload, event_name)

        self.lock = asyncio.Lock()

    def create_embed(self, payload, event_name):
        self.embed.title = f"[{payload['repository']['name']}] Workflows"
        self.embed.url = f"{payload['repository']['html_url']}/commit/{payload[event_name]['head_sha']}"
        return self.embed
    
    def determine_color(self, payload):
        if payload['conclusion'] == "failure":
            return "\N{LARGE RED CIRCLE}"
        elif payload['conclusion'] == "success":
            return "\N{LARGE GREEN CIRCLE}"
        elif payload['status'] == "queued":
            return "\N{LARGE YELLOW CIRCLE}"
        elif payload['status'] == "in_progress":
            return "<a:WindowsLoading:883414701218873376>"
        else:
            return "\N{BLACK QUESTION MARK ORNAMENT}"
        
    def create_description(self):
        desc = []
        for value in self.workflows.values():
            desc.append(f"{value['color']} {value['name']}")
        return "\n".join(desc)

    async def handle_workflow_job(self, payload):
        color = self.determine_color(payload['workflow_job'])
        # Somewhere we'll differentiate between pull_request and push
        self.workflows[payload['workflow_job']['run_id']] = {'color': color, 'name': payload['workflow_job']['workflow_name']}
        self.embed.description = self.create_description()
        async with self.lock:
            await self.message.edit(embed=self.embed)

    # Can github just be sane...
    async def handle_workflow_run(self, payload):
        if payload['workflow_run']['event'] == "workflow_dispatch":
            self.embed.title = f"[{payload['repository']['name']}] Manually Triggered Workflow"
            self.embed.set_author(name=payload['workflow_run']['triggering_actor']['login'],
                                  url=payload['workflow_run']['triggering_actor']['html_url'])
        
        if payload['workflow_run']['event'] == "pull_request":

            if payload['workflow_run']['pull_requests']:
                self.embed.url = f"{payload['repository']['html_url']}/pulls/{payload['workflow_run']['pull_requests'][0]['id']}"

            self.embed.title = f"[{payload['repository']['name']}] Pull Request Workflows"
            actor_pfp = payload['workflow_run']['triggering_actor'].get("avatar_url", None)
            self.embed.set_author(name=payload['workflow_run']['triggering_actor']['login'],
                                  url=payload['workflow_run']['triggering_actor']['html_url'],
                                  icon_url=actor_pfp)

        if self.message:
            async with self.lock:
                await self.message.edit(embed=self.embed)

    async def handle_check_run(self, payload):
        ...

    # An actions file
    async def handle_check_suite(self, payload):
        ...


@app.post("/github/{webhook_id}/{webhook_token}")
async def receive_event(payload: dict, webhook_id: int, webhook_token: str, request: Request, thread_id: Optional[int] = None):
    event = request.headers['X-GitHub-Event']
    if event not in ("workflow_job", "check_run", "workflow_run"):
        return

    id = payload[event]['head_sha']
    obj: Optional[WorkflowEmbed] = cache.get(id, None)

    if obj is None:
        obj = WorkflowEmbed(event, payload)

        if event == "workflow_run":
            await obj.handle_workflow_run(payload)

        cache[id] = obj
        webhook = discord.Webhook.partial(webhook_id, webhook_token, session=app.session)
        async with obj.lock:
            params = {}
            if thread_id:
                params['thread'] = discord.Object(thread_id)

            cache[id].message = await webhook.send(username="GitHub Actions",
                                                   avatar_url="https://cdn.discordapp.com/attachments/527965708793937960/1086369291424776383/gh.png",
                                                   embed=obj.embed, wait=True, **params)
    else:
        assert isinstance(obj, WorkflowEmbed)

        if event == "check_run":
            await obj.handle_check_run(payload)
        if event == "workflow_job":
            await obj.handle_workflow_job(payload)
