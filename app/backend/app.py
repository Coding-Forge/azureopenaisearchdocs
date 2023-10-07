import io
import logging
import mimetypes
import os
import time

import aiohttp
import openai
from azure.identity.aio import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import BlobServiceClient
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from clients.searchClient import AzureSearchClient
from clients.storageClient import StorageClient
#from phrases.extract import Key_Words
from quart_schema import QuartSchema, validate_request, validate_response
from dataclasses import dataclass

from quart import (
    Blueprint,
    Quart,
    abort,
    current_app,
    jsonify,
    request,
    send_file,
    send_from_directory,
)
from quart_schema import ResponseSchemaValidationError

from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from approaches.readdecomposeask import ReadDecomposeAsk
from approaches.readretrieveread import ReadRetrieveReadApproach
from approaches.retrievethenread import RetrieveThenReadApproach

CONFIG_OPENAI_TOKEN = "openai_token"
CONFIG_CREDENTIAL = "azure_credential"
CONFIG_ASK_APPROACHES = "ask_approaches"
CONFIG_CHAT_APPROACHES = "chat_approaches"
CONFIG_BLOB_CONTAINER_CLIENT = "blob_container_client"


bp = Blueprint("routes", __name__, static_folder='static')


@dataclass
class Chat:
    answer: str | None
    data_points: list | None
    thoughts: str | None

@dataclass
class Ask:
    answer: str
    data_points: list
    thoughts: str

@dataclass
class File:
    any: any

@bp.errorhandler(ResponseSchemaValidationError)
async def handle_response_validation_error(any):
    print(f'what is the error {any}')
    return {"error": "VALIDATION"}, 500


#@bp.route("/", methods=["GET"])
#async def index():
#    html = await bp.send_static_file("index.html")
#    print(type(html))
#    return html

#@bp.route("/favicon.ico", methods=["GET"])
#async def favicon():
#    html = await bp.send_static_file("favicon.ico")
#    print(type(html))
#    return html

#@bp.route("/assets/<path:path>", methods=["GET"])
#@validate_response(File,200)
#async def assets(path):
#    ff = await send_from_directory("static/assets", path)
#    print(type(ff))
#    return ff

# Serve content files from blob storage from within the app to keep the example self-contained.
# *** NOTE *** this assumes that the content files are public, or at least that all users of the app
# can access all the files. This is also slow and memory hungry.
#@bp.route("/content/<path>")
#@validate_response(File, status_code=200)
#async def content_file(path):
#    blob_container_client = current_app.config[CONFIG_BLOB_CONTAINER_CLIENT]
#    blob = await blob_container_client.get_blob_client(path).download_blob()
#    if not blob.properties or not blob.properties.has_key("content_settings"):
#        abort(404)
#    mime_type = blob.properties["content_settings"]["content_type"]
#    if mime_type == "application/octet-stream":
#        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
#    blob_file = io.BytesIO()
#    await blob.readinto(blob_file)
#    blob_file.seek(0)
#    return await send_file(blob_file, mimetype=mime_type, as_attachment=False, attachment_filename=path)

@bp.route("/ask", methods=["POST"])
@validate_response(Ask, 200)
async def ask():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()
    approach = request_json["approach"]
    try:
        impl = current_app.config[CONFIG_ASK_APPROACHES].get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        # Workaround for: https://github.com/openai/openai-python/issues/371
        async with aiohttp.ClientSession() as s:
            openai.aiosession.set(s)
            r = await impl.run(request_json["question"], request_json.get("overrides") or {})
        return jsonify(r)
    except Exception as e:
        logging.exception("Exception in /ask")
        return jsonify({"error": str(e)}), 500


@bp.route("/chat", methods=["POST"])
@validate_response(Chat, 200)
async def chat():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()
    approach = request_json["approach"]
    try:
        impl = current_app.config[CONFIG_CHAT_APPROACHES].get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        # Workaround for: https://github.com/openai/openai-python/issues/371
        async with aiohttp.ClientSession() as s:
            openai.aiosession.set(s)
            r = await impl.run(request_json["history"], request_json.get("overrides") or {})

            # pull out the response body to match it to the dataclass
            js = await jsonify(r).get_json()
        return js, 200
    except Exception as e:
        logging.exception("Exception in /chat")
        return jsonify({"error": str(e)}), 500

@bp.before_request
async def ensure_openai_token():
    openai_token = current_app.config[CONFIG_OPENAI_TOKEN]
    if openai_token.expires_on < time.time() + 60:
        openai_token = await current_app.config[CONFIG_CREDENTIAL].get_token("https://cognitiveservices.azure.com/.default")
        current_app.config[CONFIG_OPENAI_TOKEN] = openai_token
        openai.api_key = openai_token.token

@bp.before_app_serving
async def setup_clients():

    # Replace these with your own values, either in environment variables or directly here
    AZURE_STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
    AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER")
    AZURE_SEARCH_SERVICE = os.getenv("AZURE_SEARCH_SERVICE")
    AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
    AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")
    AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT")
    AZURE_OPENAI_CHATGPT_MODEL = os.getenv("AZURE_OPENAI_CHATGPT_MODEL")
    AZURE_OPENAI_EMB_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMB_DEPLOYMENT")

    KB_FIELDS_CONTENT = os.getenv("KB_FIELDS_CONTENT", "content")
    KB_FIELDS_SOURCEPAGE = os.getenv("KB_FIELDS_SOURCEPAGE", "sourcepage")

    # Use the current user identity to authenticate with Azure OpenAI, Cognitive Search and Blob Storage (no secrets needed,
    # just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the
    # keys for each service
    # If you encounter a blocking error during a DefaultAzureCredential resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
    azure_credential = DefaultAzureCredential(exclude_shared_token_cache_credential = True)

    #kw = Key_Words(
    #    phrase="When it is time for bed you should turn off all mobile devices and give yourself a chance to fall asleep naturally",
    #    credential=azure_credential
    #)

    #print("get to this point")
    #words = await kw.sample_extract_key_phrases_async()
    #for word in words:
    #    print(words)

    search_client = AzureSearchClient(
        service_endpoint = f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
        azure_credential=azure_credential
    )

    indx = await search_client.list_index()
    print(f'what is indx {len(indx)}')
    for i in indx:
        print(f'this is the index if found {i}')

    # Set up clients for Cognitive Search and Storage
    search_client = SearchClient(
        endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
        index_name=AZURE_SEARCH_INDEX,
        credential=azure_credential)
    blob_client = BlobServiceClient(
        account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=azure_credential)
    blob_container_client = blob_client.get_container_client(AZURE_STORAGE_CONTAINER)

    # Used by the OpenAI SDK
    openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com"
    openai.api_version = "2023-05-15"
    openai.api_type = "azure_ad"
    openai_token = await azure_credential.get_token(
        "https://cognitiveservices.azure.com/.default"
    )
    openai.api_key = openai_token.token

    # Store on app.config for later use inside requests
    current_app.config[CONFIG_OPENAI_TOKEN] = openai_token
    current_app.config[CONFIG_CREDENTIAL] = azure_credential
    current_app.config[CONFIG_BLOB_CONTAINER_CLIENT] = blob_container_client

    # Various approaches to integrate GPT and external knowledge, most applications will use a single one of these patterns
    # or some derivative, here we include several for exploration purposes
    current_app.config[CONFIG_ASK_APPROACHES] = {
        "rtr": RetrieveThenReadApproach(
            search_client,
            AZURE_OPENAI_CHATGPT_DEPLOYMENT,
            AZURE_OPENAI_CHATGPT_MODEL,
            AZURE_OPENAI_EMB_DEPLOYMENT,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT
        ),
        "rrr": ReadRetrieveReadApproach(
            search_client,
            AZURE_OPENAI_CHATGPT_DEPLOYMENT,
            AZURE_OPENAI_EMB_DEPLOYMENT,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT
        ),
        "rda": ReadDecomposeAsk(search_client,
            AZURE_OPENAI_CHATGPT_DEPLOYMENT,
            AZURE_OPENAI_EMB_DEPLOYMENT,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT
        )
    }
    current_app.config[CONFIG_CHAT_APPROACHES] = {
        "rrr": ChatReadRetrieveReadApproach(
            search_client,
            AZURE_OPENAI_CHATGPT_DEPLOYMENT,
            AZURE_OPENAI_CHATGPT_MODEL,
            AZURE_OPENAI_EMB_DEPLOYMENT,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT,
        )
    }


def create_app():
    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        configure_azure_monitor()
        AioHttpClientInstrumentor().instrument()
    
    app = Quart(__name__)
    QuartSchema(app)    
    app.register_blueprint(bp)
    app.asgi_app = OpenTelemetryMiddleware(app.asgi_app)

    return app
