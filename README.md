# HugCare HA Integration

This repository contains the Home Assistant custom integration for HugCare Gateway.

## What It Does

- Provides HACS-installable integration metadata
- Exposes a config flow and options flow in Home Assistant UI
- Stores settings from config entries into:
  - `/config/hugcare_gateway/runtime_config.json`

The NetDaemon runtime reads this runtime config file.

## Structure

- `hacs.json`
- `custom_components/hugcare_gateway/`
  - `manifest.json`
  - `config_flow.py`
  - `__init__.py`
  - translations files

## Install (HACS)

1. Add this repository as a custom repository in HACS (`Integration` type).
2. Install `HugCare Gateway`.
3. Restart Home Assistant.
4. Go to `Settings > Devices & Services > Add Integration` and add `HugCare Gateway`.

## Runtime Pairing

This project is paired with the NetDaemon runtime repository.
The runtime consumes `/config/hugcare_gateway/runtime_config.json` produced by this integration.
