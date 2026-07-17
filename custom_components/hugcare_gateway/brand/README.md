# HugCare Gateway Brand Assets

Place integration brand images in this folder.

Required files:
- icon.png (square icon)
- logo.png (horizontal logo)

Optional dark-mode variants:
- icon_dark.png
- logo_dark.png

After adding images:
1) Commit and push this repository.
2) In Home Assistant HACS, Redownload the integration.
3) Restart Home Assistant.

Verify image endpoints after restart:
- /api/brands/integration/hugcare_gateway/icon?placeholder=no
- /api/brands/integration/hugcare_gateway/logo?placeholder=no
