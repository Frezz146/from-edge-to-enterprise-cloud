"""Local inference loop: download, load, and chat with a small language model
entirely in-process using the Foundry Local SDK's native chat completions API.

No REST server, no cloud endpoint, no per-token cost - every request in this
file is served by execution providers running on your own machine.
"""

from foundry_local_sdk import Configuration, FoundryLocalManager

MODEL_ALIAS = "qwen2.5-0.5b"
APP_NAME = "frezz_tech_edge_agent"


def download_and_register_eps(manager: FoundryLocalManager) -> None:
    """Discover local hardware accelerators (NPU/GPU/CPU) and pull the
    matching ONNX Runtime execution providers into the local cache."""
    current_ep = ""

    def ep_progress(ep_name: str, percent: float) -> None:
        nonlocal current_ep
        if ep_name != current_ep:
            if current_ep:
                print()
            current_ep = ep_name
        print(f"\r  {ep_name:<30}  {percent:5.1f}%", end="", flush=True)

    print("[Hardware] Discovering and registering execution providers...")
    manager.download_and_register_eps(progress_callback=ep_progress)
    if current_ep:
        print()


def run_local_loop() -> None:
    # 1. Initialize the singleton runtime for this process.
    config = Configuration(app_name=APP_NAME)
    print("[System] Initializing in-process Foundry Local runtime...")
    FoundryLocalManager.initialize(config)
    manager = FoundryLocalManager.instance

    download_and_register_eps(manager)

    # 2. Resolve the model alias to a hardware-optimized variant and cache it.
    model = manager.catalog.get_model(MODEL_ALIAS)
    print(f"\n[Model] Preparing resources for '{MODEL_ALIAS}'...")
    if not model.is_cached:
        print(f"[Model] Downloading '{MODEL_ALIAS}' weights from the cloud-hosted catalog...")
        model.download(
            lambda progress: print(
                f"\rDownload Progress: {progress:.1f}%", end="", flush=True
            )
        )
        print()
    else:
        print("[Model] Found cached weights. Skipping download.")

    # 3. Load the model into memory (allocates VRAM/RAM, builds the runtime session).
    print("[Model] Loading model into memory...")
    model.load()

    # 4. Get a native chat client and run a streaming completion in-process.
    client = model.get_chat_client()
    # Enforce deterministic output so repeated runs are directly comparable.
    client.settings.temperature = 0.0
    client.settings.max_tokens = 256

    messages = [
        {
            "role": "user",
            "content": "Describe the advantages of developer-local AI models in three bullets.",
        }
    ]

    print(f"\n[User]: {messages[0]['content']}\n")
    print("[Assistant]: ", end="", flush=True)
    for chunk in client.complete_streaming_chat(messages):
        if not chunk.choices:
            continue
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)
    print("\n")

    # 5. Graceful cleanup - free the loaded weights and runtime resources.
    print("[System] Unloading model and releasing system resources...")
    model.unload()
    print("[System] Local inference loop completed successfully.")


if __name__ == "__main__":
    run_local_loop()
