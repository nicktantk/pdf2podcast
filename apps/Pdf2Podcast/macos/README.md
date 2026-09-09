# Pdf2Podcast for Mac

Mac Catalyst builds the shared iOS player as a separate Mac app. The Mac importer
explicitly presents a copy-based document picker and stages selected PDFs in
private temporary storage before uploading them. Original PDFs are not modified.

## Build

Install Xcode and XcodeGen. From this directory:

```sh
xcodegen generate
open Pdf2PodcastMac.xcodeproj
```

Select your own development team in Xcode. To build from a shell, set
`APPLE_DEVELOPMENT_TEAM` to that team's identifier, then run:

```sh
xcodebuild -project Pdf2PodcastMac.xcodeproj -scheme Pdf2PodcastMac \
  -destination 'platform=macOS,variant=Mac Catalyst' -configuration Release \
  DEVELOPMENT_TEAM="$APPLE_DEVELOPMENT_TEAM" \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  -derivedDataPath /tmp/Pdf2PodcastMacDerived build
```

The application is built at
`/tmp/Pdf2PodcastMacDerived/Build/Products/Release-maccatalyst/Pdf2Podcast.app`.
After installing it, use connection settings to enter your backend URL and mobile
token. Settings are private to each device, while conversions share the backend
library. No provider API key or prefilled mobile token is included.

The Mac sandbox permits network access and read-only access to user-selected
files. Its keychain access group is separate from the iPhone app's group.
