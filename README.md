<div align="center">

# 🛠 Sydka 

Automated patched iOS SDK ready for Theos tweak development.

![iOS 9.3 → 26.x](https://img.shields.io/badge/iOS-9.3%20→%2026.x-blue)
![GitHub Actions](https://img.shields.io/badge/CI-GitHub%20Actions-purple)

## Pipeline

```
Map SDKs (sdk_map.json) › Xcode (xcodes) › IPSW (aria2) › dyld cache (arm64(e)) › tbd stubs (leptos-null/tbd) › Release (.sdk.tar.xz)
```


## Usage

### Install from Releases

```sh
# Download from Releases, then:
tar -xJf iPhoneOS18.2.sdk.tar.xz -C $THEOS/sdks/
```

### Build locally (macOS + Homebrew)

```sh
./build_sdk.sh --ios 18.2
./build_sdk.sh --all    # every SDK
```


## Secrets Required

| Secret | Description |
|---|---|
| `APPLE_ID` | Your Apple ID email — passed as `FASTLANE_USER` |
| `FASTLANE` | Session cookie from `fastlane spaceauth` · expires ~30 days |

### Generate `FASTLANE_SESSION`

```sh
brew install ruby
gem install fastlane --no-document
fastlane spaceauth -u you@apple.com
# Copy the output → paste into FASTLANE secret
```
