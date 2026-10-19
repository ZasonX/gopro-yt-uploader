# GoPro → YouTube 批次上傳工具

把一整個資料夾的 GoPro 影片一次上傳到 YouTube，每支影片自動設定好：

| 項目 | 內容 |
|---|---|
| 標題 | 拍攝的當地時間，例如 `2026-03-15 14:32`（格式可選） |
| 說明 | 原始檔名（例如 `GX010123.MP4`），範本可改 |
| 類別 | 旅遊與活動 |
| 拍攝日期 | 拍攝當天 |
| 拍攝地點 | GoPro GPS 座標 + 地名（OpenStreetMap 反查，可在地圖上改） |
| 為兒童打造 | 否 |
| 付費宣傳 | 否 |
| AI / 合成內容 | 否 |
| 播放清單 | 例如「2025 日本」，不存在會自動建立 |
| 瀏覽權限 | 非公開（見下方「已知限制」） |

## 特色

- **分段自動合併**：GoPro 長片被切成 `GX01xxxx`、`GX02xxxx`… 的分段，會自動認成同一段錄影，用 ffmpeg 無損合併成一支再上傳（可關閉，改成分開上傳並加上 `(1/3)` 編號）。
- **時間自動校正**：讀取影片內的 GPS 時間，算出相機時鐘和 UTC 的差。就算相機還設在台灣時間，標題也會是日本當地時間；沒有 GPS 的影片也套用同一個校正值。
- **地點**：有 GPS 的影片直接用 GPS 座標；沒有 GPS 的，沿用 3 小時內最接近那段的位置（標示為「推測」）。也可以在地圖上點選，或搜尋地名。
- **不重複上傳**：上傳紀錄存在 `%APPDATA%\GoProYTUploader\state.json`，重新掃描時已上傳的會自動取消勾選。另外會寫一份 `upload_history.csv`（可用 Excel 開），每支影片附 YouTube 連結。
- **斷線重試**：上傳中斷會自動重試；配額用完會停下來，隔天再按一次「開始上傳」就會接著傳。
- 不需要另外安裝 ffprobe 或 exiftool，時間和 GPS 由程式直接解析 MP4。

## 第一次設定（約 10 分鐘，只要做一次）

### 1. 建立 Google Cloud 專案並啟用 API

1. 打開 <https://console.cloud.google.com/>，在左上角建立一個新專案，名稱隨意，例如 `gopro-uploader`。
2. 進入「API 和服務 → 程式庫」，搜尋 **YouTube Data API v3**，按「啟用」。

### 2. 設定 OAuth 同意畫面

1. 進入「API 和服務 → OAuth 同意畫面」（新版介面叫 Google Auth Platform）。
2. 使用者類型選「外部」，填寫應用程式名稱，以及你自己的 email。
3. 在「目標對象 / 測試使用者」加入你的 Gmail（就是 YouTube 頻道的帳號）。

> 專案停在「測試中」狀態時，登入大約 7 天就會過期，到時候程式會請你重新登入一次。
> 不想每週重登的話，可以在「目標對象」按「發布應用程式」。自己用的話，登入時會出現「Google 尚未驗證這個應用程式」的警告，按「進階 → 繼續」即可。

### 3. 建立用戶端憑證

1. 進入「API 和服務 → 憑證 → 建立憑證 → OAuth 用戶端 ID」。
2. 應用程式類型選 **電腦版應用程式**（Desktop app）。
3. 建立後按「下載 JSON」。

### 4. 在程式裡匯入

1. 解壓縮下載的 `GoProYTUploader` 資料夾，雙擊裡面的 `GoProYTUploader.exe`，不用另外安裝 Python。
   - 第一次執行時，Windows SmartScreen 可能會顯示「Windows 已保護您的電腦」，按「其他資訊 → 仍要執行」即可。原因是這個程式沒有付費的程式碼簽章。
   - 整個資料夾要放在一起，不能只把 exe 單獨複製出來。
2. 按「設定 → 匯入 client_secret.json」，選剛剛下載的檔案。
3. 按右上角「登入」，在跳出的瀏覽器頁面登入 Google，並允許存取。

憑證和登入資訊只存在 `%APPDATA%\GoProYTUploader`，不會進到 git。

## 平常使用

1. 「選擇…」影片資料夾，程式會自動掃描並列出每一段錄影。
2. 確認播放清單名稱、旅程時區、標題格式。
3. 按「查詢地名」，把 GPS 座標換成地名。
4. 檢查表格：標題可以直接改；選取一列或多列後，可以在右側改地點（地圖點選或搜尋）。
5. 按「開始上傳」。
6. 全部完成後，到 YouTube Studio 全選這批影片，用「編輯 → 瀏覽權限 → 非公開」一次改好（原因見下方）。

## 已知限制

- **影片會先變成「私人」**：Google 規定，沒有通過審核的 API 專案上傳的影片，一律強制設為私人。程式送出的是「非公開」，如果之後專案通過審核，就會直接生效。在那之前，請照上面第 6 步在 Studio 批次改。
- **每日配額**：預設每天 10,000 單位。依 Google 2025-12 的修訂紀錄，每上傳一支影片約用 100 單位（以前是 1,600），請以 Cloud Console 的配額頁面為準。配額在太平洋時間午夜重置（台灣時間下午 3 點或 4 點）。
- **YouTube 上的拍攝地點**：API 寫入的是座標加地名文字。這些資料在 Studio 裡會怎麼顯示，要用真實上傳確認。
- **營利開關**：營利開關 API 改不了，但付費宣傳、為兒童打造這兩項都會設為「否」。

## 開發

需要 Python 3.10 以上版本。

```bash
run.bat                                        # 從原始碼執行（第一次會建立 .venv 並安裝套件）
.venv\Scripts\python.exe -m pytest -q          # 測試（會自己產生假的 GoPro MP4）
.venv\Scripts\python.exe -m gopro_uploader --debug   # 開啟視窗並啟用開發者工具
build.bat                                      # 打包成 dist\GoProYTUploader\GoProYTUploader.exe
```

`build.bat` 使用 PyInstaller，產生的是資料夾形式，約 116 MB，其中 ffmpeg 佔大部分。發布時把 `dist\GoProYTUploader` 整個資料夾壓縮成 zip 即可。圖示由 `packaging/make_icon.py` 產生。

程式結構：

| 檔案 | 用途 |
|---|---|
| `gopro_uploader/mp4meta.py` | 解析 MP4 和 GoPro GPMF（GPS5 / GPS9） |
| `gopro_uploader/scanner.py` | 找檔案、合併分段 |
| `gopro_uploader/planner.py` | 計算時間、時區校正、標題、地點推測 |
| `gopro_uploader/geocode.py` | OpenStreetMap 地名查詢（含快取，每秒最多 1 次） |
| `gopro_uploader/youtube.py` | OAuth 登入、播放清單、可續傳上傳 |
| `gopro_uploader/merge.py` | 呼叫 ffmpeg 無損合併 |
| `gopro_uploader/app.py`、`ui/index.html` | 視窗介面（pywebview + Leaflet 地圖） |
