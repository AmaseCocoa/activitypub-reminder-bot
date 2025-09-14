import asyncio
import logging
import re
import uuid
import os
from datetime import timedelta, datetime
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization as crypto_serialization

from apkit.config import AppConfig
from apkit.server import ActivityPubServer
from apkit.server.types import Context, ActorKey
from apkit.server.responses import ActivityResponse
from apkit.models import (
    Actor, Application, CryptographicKey, Follow, Create, Note, Mention, Actor as APKitActor, OrderedCollection,
)
from apkit.client import WebfingerResource, WebfingerResult, WebfingerLink
from apkit.client.asyncio.client import ActivityPubClient

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
HOST = "apsig.amase.cc"
USER_ID = "reminder"

# --- Key Persistence ---
KEY_FILE = "private_key.pem"

if os.path.exists(KEY_FILE):
    logger.info(f"Loading existing private key from {KEY_FILE}.")
    with open(KEY_FILE, "rb") as f:
        private_key = crypto_serialization.load_pem_private_key(f.read(), password=None)
else:
    logger.info(f"No key file found. Generating new private key and saving to {KEY_FILE}.")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(KEY_FILE, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=crypto_serialization.Encoding.PEM,
            format=crypto_serialization.PrivateFormat.PKCS8,
            encryption_algorithm=crypto_serialization.NoEncryption()
        ))

public_key_pem = private_key.public_key().public_bytes(
    encoding=crypto_serialization.Encoding.PEM,
    format=crypto_serialization.PublicFormat.SubjectPublicKeyInfo
).decode('utf-8')

# --- Actor Definition (Reminder Bot) ---
actor = Application(
    id=f"https://{HOST}/actor",
    name="Reminder Bot",
    preferredUsername=USER_ID,
    summary="A bot that sends you reminders. Mention me like: @reminder 5m Check the oven",
    inbox=f"https://{HOST}/inbox",
    outbox=f"https://{HOST}/outbox",
    publicKey=CryptographicKey(
        id=f"https://{HOST}/actor#main-key",
        owner=f"https://{HOST}/actor",
        publicKeyPem=public_key_pem
    )
)


# --- Key Retrieval Function ---
async def get_keys_for_actor(identifier: str) -> list[ActorKey]:
    if identifier == actor.id:
        return [ActorKey(key_id=actor.publicKey.id, private_key=private_key)] # type: ignore
    return []

# --- Server Initialization ---
app = ActivityPubServer(apkit_config=AppConfig(
    actor_keys=get_keys_for_actor
))

# --- In-memory Store for Activities ---
ACTIVITY_STORE = {}
CACHE = {}
CACHE_TTL = timedelta(minutes=5)

# --- Reminder Parsing Logic ---
def parse_reminder(text: str) -> tuple[timedelta | None, str | None, str | None]:
    """
    Parses reminder text like '5m do something'.
    Returns (delay, message, original_time_string).
    """
    pattern = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*(.*)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None, None, None

    value, unit, message = match.groups()
    value = int(value)
    original_time_string = f"{value}{unit.lower()}"
    
    unit = unit.lower()
    if unit == 's':
        delta = timedelta(seconds=value)
    elif unit == 'm':
        delta = timedelta(minutes=value)
    elif unit == 'h':
        delta = timedelta(hours=value)
    elif unit == 'd':
        delta = timedelta(days=value)
    else:
        return None, None, None
        
    if not message.strip():
        message = "Here's your reminder!"

    return delta, message.strip(), original_time_string

async def send_reminder(ctx: Context, delay: timedelta, message: str, target_actor: APKitActor, original_note: Note):
    """Waits for a delay and then sends a reminder."""
    logger.info(f"Scheduling reminder for {target_actor.id} in {delay}: '{message}'")
    await asyncio.sleep(delay.total_seconds())
    
    logger.info(f"Sending reminder to {target_actor.id}")
    
    try:
        async with ActivityPubClient() as client:
            target_actor = await client.actor.fetch(target_actor.id) # type: ignore
    except Exception:
        pass

    
    mention_tag = Mention(href=target_actor.id, name=f"@{target_actor.preferredUsername}")
    
    reminder_note = Note(
        id=f"https://{HOST}/notes/{uuid.uuid4()}",
        attributedTo=actor.id,
        inReplyTo=original_note.id, # type: ignore
        content=f"<p>🔔 Reminder for {f'<p><span class="h-card" translate="no"><a href="{target_actor.url}" class="u-url mention">@<span>{target_actor.preferredUsername}</span></a></span></p>'}: {message}</p>", # type: ignore
        to=[target_actor.id], # type: ignore
        tag=[mention_tag] # type: ignore
    )
    
    reminder_create = Create(
        id=f"https://{HOST}/creates/{uuid.uuid4()}",
        actor=actor.id,
        object=reminder_note,
        to=[target_actor.id] # type: ignore
    )
    
    ACTIVITY_STORE[reminder_note.id] = reminder_note
    ACTIVITY_STORE[reminder_create.id] = reminder_create
    
    keys = await get_keys_for_actor(f"https://{HOST}/actor")
    await ctx.send(keys, target_actor, reminder_create)
    logger.info(f"Reminder sent to {target_actor.id}")

# --- Endpoints ---
app.inbox("/inbox")

@app.get("/actor")
async def get_actor_endpoint():
    return ActivityResponse(actor)

@app.webfinger()
async def webfinger_endpoint(request: Request, acct: WebfingerResource) -> Response:
    if not acct.url:
        if acct.username == USER_ID and acct.host == HOST:
            link = WebfingerLink(rel="self", type="application/activity+json", href=actor.id) # type: ignore
            wf_result = WebfingerResult(subject=acct, links=[link])
            return JSONResponse(wf_result.to_json(), media_type="application/jrd+json")
    else:
        if acct.url == f"https://{HOST}/actor":
            link = WebfingerLink(rel="self", type="application/activity+json", href=actor.id) # type: ignore
            wf_result = WebfingerResult(subject=acct, links=[link])
            return JSONResponse(wf_result.to_json(), media_type="application/jrd+json")
    return JSONResponse({"message": "Not Found"}, status_code=404)

@app.get("/outbox")
async def get_outbox_endpoint():
    """Returns the outbox collection."""
    outbox_collection = OrderedCollection(
        id=actor.outbox, # type: ignore
        totalItems=0,
        orderedItems=[]
    )
    return ActivityResponse(outbox_collection)

@app.get("/notes/{note_id}")
async def get_note_endpoint(note_id: uuid.UUID):
    """Returns a specific note."""
    note_uri = f"https://{HOST}/notes/{note_id}"
    
    # Check cache first
    if note_uri in CACHE and (datetime.now() - CACHE[note_uri]["timestamp"]) < CACHE_TTL:
        return ActivityResponse(CACHE[note_uri]["activity"])
        
    if note_uri in ACTIVITY_STORE:
        activity = ACTIVITY_STORE[note_uri]
        CACHE[note_uri] = {"activity": activity, "timestamp": datetime.now()}
        return ActivityResponse(activity)
        
    return Response(status_code=404)

@app.get("/creates/{create_id}")
async def get_create_endpoint(create_id: uuid.UUID):
    """Returns a specific create activity."""
    create_uri = f"https://{HOST}/creates/{create_id}"
    
    # Check cache first
    if create_uri in CACHE and (datetime.now() - CACHE[create_uri]["timestamp"]) < CACHE_TTL:
        return ActivityResponse(CACHE[create_uri]["activity"])
        
    if create_uri in ACTIVITY_STORE:
        activity = ACTIVITY_STORE[create_uri]
        CACHE[create_uri] = {"activity": activity, "timestamp": datetime.now()}
        return ActivityResponse(activity)
        
    return Response(status_code=404)

# --- Activity Handlers ---

@app.on(Follow)
async def on_follow_activity(ctx: Context):
    """Automatically accept all follow requests."""
    activity = ctx.activity
    if not isinstance(activity, Follow): 
        return Response(status_code=400)
    
    async with ActivityPubClient() as client:
        follower_actor = await client.actor.fetch(activity.actor.id if isinstance(activity.actor, Actor) else activity.actor) # pyright: ignore[reportArgumentType, reportAttributeAccessIssue]
        if not follower_actor: 
            return Response(status_code=400)

    accept_activity = activity.accept(
        id=f"https://{HOST}/activity/follows/{follower_actor}",
        actor=actor
    )
    keys = await get_keys_for_actor(f"https://{HOST}/actor")
    await ctx.send(keys, follower_actor, accept_activity) # type: ignore
    logger.info(f"Accepted follow from {follower_actor.id}") # type: ignore
    return Response(status_code=202)

@app.on(Create)
async def on_create_activity(ctx: Context):
    activity = ctx.activity
    if not (isinstance(activity, Create) and isinstance(activity.object, Note)):
        return Response(status_code=202)

    note = activity.object
    
    is_mentioned = False
    if note.tag:
        for tag in note.tag:
            if isinstance(tag, Mention) and tag.href == actor.id:
                is_mentioned = True
                break
    
    if not is_mentioned:
        return Response(status_code=202)

    logger.info(f"Received mention from {activity.actor}: {note.content}")

    clean_content = re.sub('<[^<]+?>', '', note.content) # type: ignore
    bot_mention_str = f"@{actor.preferredUsername}"
    command_text = clean_content.replace(bot_mention_str, "").strip()

    delay, message, time_str = parse_reminder(command_text)
    
    async with ActivityPubClient() as client:
        sender_actor = await client.actor.fetch(activity.actor) # type: ignore

    if delay and message and sender_actor:
        asyncio.create_task(send_reminder(ctx, delay, message, sender_actor, note)) # type: ignore
        reply_content = f"<p>✅ OK! I'll remind you in {time_str}.</p>"
    else:
        reply_content = "<p>🤔 Sorry, I didn't understand. Use format: <code>@reminder [time] [message]</code>.</p><p>Example: <code>@reminder 10m Check the oven</code></p>"

    reply_note = Note(
        id=f"https://{HOST}/notes/{uuid.uuid4()}",
        attributedTo=actor.id,
        inReplyTo=note.id, # type: ignore
        content=reply_content,
        to=[sender_actor.id], # type: ignore
        tag=[Mention(href=sender_actor.id, name=f"@{sender_actor.preferredUsername}")] # type: ignore
    )
    reply_create = Create(
        id=f"https://{HOST}/creates/{uuid.uuid4()}",
        actor=actor.id,
        object=reply_note,
        to=[sender_actor.id] # type: ignore
    )
    
    ACTIVITY_STORE[reply_note.id] = reply_note
    ACTIVITY_STORE[reply_create.id] = reply_create
    
    keys = await get_keys_for_actor(f"https://{HOST}/actor")
    await ctx.send(keys, sender_actor, reply_create) # type: ignore
    
    return Response(status_code=202)

# --- Main execution ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting uvicorn server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
