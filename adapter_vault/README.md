# Adapter Vault

Storage for trained LoRA adapters. Each adapter is a domain-specific weight
matrix that sits on top of a frozen base Qwen 7B model.

## Structure

```
adapter_vault/
  index.json           # Master manifest of all adapters
  python-testing/
    v1/                # First trained version
      adapter_config.json
      adapter_model.safetensors
    v2/                # Improved version
  git-operations/
    v1/
```

## Index format (index.json)

```json
{
  "python-testing": {
    "latest_version": 2,
    "versions": {
      "1": {
        "created": "2026-06-11T...",
        "trajectory_count": 50,
        "base_model": "qwen2.5:7b",
        "rank": 16,
        "eval_score": 42.0,
        "notes": "Initial training from 50 trajectories"
      }
    }
  }
}
```

## Loading

The Director loads the latest adapter for a domain before routing tasks.
Adapters are loaded via Ollama's Modelfile or direct Hugging Face PEFT loading.
