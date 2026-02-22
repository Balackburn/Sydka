<div align="center">

<!-- Logo -->
<pre>
 ███████╗██╗   ██╗██████╗ ██╗  ██╗ █████╗
 ██╔════╝╚██╗ ██╔╝██╔══██╗██║ ██╔╝██╔══██╗
 ███████╗ ╚████╔╝ ██║  ██║█████╔╝ ███████║
 ╚════██║  ╚██╔╝  ██║  ██║██╔═██╗ ██╔══██║
 ███████║   ██║   ██████╔╝██║  ██╗██║  ██║
 ╚══════╝   ╚═╝   ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
</pre>

### Automated Patched iOS SDKs  
**Ready for seamless Theos tweak development**

<br>

<a href="https://theos.dev">
  <img src="https://img.shields.io/badge/Built%20for-Theos-0A84FF?style=for-the-badge&logo=apple&logoColor=white" />
</a>
<a href="#">
  <img src="https://img.shields.io/badge/iOS-9.3%20→%2026.x-111827?style=for-the-badge&logo=apple&logoColor=white" />
</a>
<a href="#">
  <img src="https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?style=for-the-badge&logo=github-actions&logoColor=white" />
</a>
<a href="LICENSE">
  <img src="https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge" />
</a>

<br><br>


</div>

---

## Pipeline

Each SDK is built fully automatically through the following stages:

```
sdk_map.json  ›  xcodes  ›  aria2 (IPSW)  ›  dyld cache (arm64/e)  ›  tbd stubs  ›  .sdk.tar.xz
     │               │           │                    │                     │               │
  Map target       Fetch       Pull firmware      Extract symbols       Generate stubs   Release
  iOS version      Xcode       from Apple CDN     for arch target      via leptos/tbd    artifact
```

---

## Usage

### Install from Releases

Download the `.sdk.tar.xz` archive for your target iOS version from the [Releases](../../releases) page, then extract it directly into your Theos SDK directory:

```sh
tar -xJf iPhoneOS18.2.sdk.tar.xz -C $THEOS/sdks/
```

### Build Locally

> **Requirements:** macOS · Homebrew

```sh
# Build a specific iOS version
./build_sdk.sh --ios 18.2

# Build every SDK defined in sdk_map.json
./build_sdk.sh --all
```

---

## Secrets

The CI pipeline requires two repository secrets to authenticate with Apple:

| Secret | Description |
|---|---|
| `APPLE_ID` | Your Apple ID email — used as `FASTLANE_USER` |
| `FASTLANE` | Session cookie from `fastlane spaceauth` — expires every ~30 days |

### Generating your `FASTLANE_SESSION`

```sh
brew install ruby
gem install fastlane --no-document

fastlane spaceauth -u you@apple.com
# → Copy the printed session string and paste it into the FASTLANE secret
```

> [!WARNING]
> The `FASTLANE` session expires roughly every 1-30 days. Re-run `spaceauth` and update the secret to keep the pipeline functional.

---

## SDK Map

Target iOS versions are declared in [`sdk_map.json`](sdk_map.json). Add an entry there to include a new version in the `--all` build.

---

<div align="center">

<sub>Built with GitHub Actions · Powered by <a href="https://theos.dev">Theos</a></sub>

</div>
