# Privacy Policy / 隱私權政策

GoPro YT Uploader is a desktop app that runs only on your own computer. There is no server run by the developer: the developer never receives your videos, your Google account data or anything else.

GoPro YT Uploader 是只在你自己電腦上執行的桌面程式。開發者沒有任何伺服器，收不到你的影片、Google 帳號資料或其他任何東西。

## What the app accesses / 程式會存取什麼

- **Your YouTube account** (Google OAuth, scope `https://www.googleapis.com/auth/youtube`), only after you sign in and only to do what you start in the app:
  - upload the videos you pick and fill in their settings (title, description, privacy, playlist…);
  - when you ask for a comparison or a check, read your channel's videos and playlists to see which files are already uploaded;
  - change the title / description / settings of videos you pick.

  The app never deletes videos, posts comments, or does anything you didn't start.
- **The video files you pick**, read on your computer (time and GPS data in the files are used for titles and place names).

- **你的 YouTube 帳號**（Google OAuth，權限 `youtube`）：只有在你登入後、而且只做你在程式裡按下去的事：上傳你選的影片並填好設定；你要求比對或核對時，讀取頻道的影片和播放清單；修改你選的影片的標題、說明和設定。程式不會刪除影片、留言，或做任何你沒有要求的事。
- **你選的影片檔**，只在你的電腦上讀取（檔案裡的時間和 GPS 用來產生標題和地名）。

## Where data goes / 資料會送到哪裡

- **Google / YouTube**: your videos and their settings, through the YouTube Data API.
- **OpenStreetMap**: GPS coordinates from your videos, and place names you search for, are sent to OpenStreetMap's Nominatim service to look up place names; the map loads tiles from OpenStreetMap's tile servers, and its map library (Leaflet) from unpkg.com. See the [OSMF privacy policy](https://osmfoundation.org/wiki/Privacy_Policy).
- Nothing else: no analytics, no tracking, no ads.

- **Google / YouTube**：透過 YouTube Data API 傳送你的影片和設定。
- **OpenStreetMap**：影片裡的 GPS 座標和你搜尋的地名，會送到 OpenStreetMap 的 Nominatim 服務查地名；地圖從 OpenStreetMap 的伺服器載入，地圖程式庫（Leaflet）從 unpkg.com 載入。
- 除此之外沒有：沒有分析、追蹤或廣告。

## What is stored, and where / 存了什麼、存在哪裡

Only on your computer, in the app data folder (`%APPDATA%\GoProYTUploader` on Windows, `~/Library/Application Support/GoProYTUploader` on Mac, `~/.local/share/GoProYTUploader` on Linux): settings, your sign-in token, upload records, the last comparison with YouTube, cached place names and run logs. Deleting that folder removes all of it.

只存在你的電腦的程式資料夾（Windows 是 `%APPDATA%\GoProYTUploader`）：設定、登入權杖、上傳紀錄、上次和 YouTube 比對的結果、地名快取和執行紀錄。刪除這個資料夾就全部清除。

Signing out in the app deletes the sign-in token from your computer. To remove the app's access to your Google account itself, go to <https://myaccount.google.com/permissions>.

在程式裡登出會刪除電腦上的登入權杖；要撤銷程式對 Google 帳號的存取權，請到 <https://myaccount.google.com/permissions> 移除。

## Google API Services User Data Policy

The app's use of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements. It also follows the [YouTube API Services Terms of Service](https://developers.google.com/youtube/terms/api-services-terms-of-service); by using it you agree to the [YouTube Terms of Service](https://www.youtube.com/t/terms) and the [Google Privacy Policy](https://policies.google.com/privacy).

## Contact / 聯絡

Questions: open an issue at <https://github.com/ZasonX/gopro-yt-uploader/issues>.

有問題請到 GitHub 開 issue。
