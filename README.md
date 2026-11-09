# GoPro → YouTube 批次上傳工具

把一整個資料夾的 GoPro 影片一次上傳到 YouTube，每支影片自動設定好：

| 項目 | 內容 |
|---|---|
| 標題 | 拍攝的當地時間，例如 `2026-03-15 14:32`（格式可選）；時區和家裡不同時加上縮寫，例如 ` JST` |
| 說明 | 原始檔名（例如 `GX010123.MP4`），範本可改 |
| 類別 | 旅遊與活動 |
| 拍攝日期 | 拍攝當天 |
| 語言 | 中文（台灣） |
| 為兒童打造 | 否 |
| 付費宣傳 | 否 |
| AI / 合成內容 | 否 |
| 播放清單 | 從頻道現有的清單選，或在程式裡建立新的；也可以不加入 |
| 瀏覽權限 | 非公開 |

拍攝地點不會送出，YouTube 已經不接受用程式設定（見「已知限制」）。

## 支援的系統

| 系統 | 狀態 |
|---|---|
| Windows 10 / 11 | 實際使用過 |
| Mac（Apple 晶片：M1 以後） | 有打包，自動測試會過，**但沒有人實際開起來用過** |
| Linux（x86-64） | 有打包，自動測試會過，**但沒有人實際開起來用過** |

Mac / Linux 版是照 Windows 版改的：檔案總管換成 Finder / 系統的檔案管理員，工作列進度條在這兩個系統上沒有。視窗、登入、上傳、合併分段理論上一樣，但可能有沒發現的問題，碰到請到 [Issues](https://github.com/ZasonX/gopro-yt-uploader/issues) 回報。

## 特色

- **分段自動合併**：GoPro 長片被切成 `GX01xxxx`、`GX02xxxx`… 的分段，會自動認成同一段錄影，用 ffmpeg 無損合併成一支再上傳（可關閉，改成分開上傳並加上 `(1/3)` 編號）。
- **時間自動校正**：依影片內的 GPS 時間和檔案時間，判斷每段錄影當時的時區，標題用當地時間。相機時鐘被重設的影片，會依檔案編號推測日期，標題只放日期。
- **地點參考**：有 GPS 的影片用座標查出地名；沒有 GPS 的，沿用 3 小時內最接近那段的位置（標示為「推測」）。也可以在地圖上點選、搜尋地名，或沿用上一支的地點（包括在 Studio 設定好的）。地名可以複製，貼到 Studio 的「影片地點」搜尋。
- **同時上傳多支，順序不亂**：YouTube 每支只給約 3.5 MB/s，所以預設同時傳 3 支（可選 1–8）。完成順序和播放清單順序都會照表格，Studio 內容列表的順序也就跟著對。
- **不重複上傳**：上傳紀錄存在程式資料夾的 `state.json`（位置見「第一次設定」第 4 步），重新掃描時已上傳的會自動取消勾選。另外會寫一份 `upload_history.csv`（可用 Excel 開），每支影片附 YouTube 連結。
- **和 YouTube 比對**：想知道資料夾裡哪些已經傳過（包括手動上傳、複製過的檔案），可以按「和 YouTube 比對…」。掃描資料夾時不會讀你的頻道，要按了、選好範圍才會讀。
- **手動上傳過的影片**：比對時，以前在 Studio 手動上傳、檔名和大小都和這個資料夾相同的影片：
  - 有填標題的，記為「已上傳・手動」，和程式傳的一樣顯示 YouTube 上的資料、可以改了再更新、核對也會檢查。
  - 什麼都沒填的（標題還是預設的）標成「草稿」，可以在表格選取後按「修正到 YouTube…」（或比對結果的「修正手動上傳的影片…」）一次補上標題、說明、類別、語言、拍攝日期等設定，並依拍攝順序加進播放清單。每支約用 100 配額單位，一天約能修 95 支，用完會停下，隔天再按一次接著修。
- **從標題判斷時區**：沒有 GPS 時，檔案只看得出相機設的時區；出國沒改相機時區的話，程式會以為還在家裡。已上傳影片的標題寫了「09:00 JST」這種時區縮寫，程式就照它判斷在哪裡；沒寫縮寫的當作家裡的時間。前後的錄影跟著最近的標題（兩邊相同，或同一天）。
- **標題時間檢查**：已上傳影片的標題開頭的日期時間和檔案不同（手打錯、照抄上一支，或相機是家裡的時間卻標了當地時區），會標出「標題時間不符」，可以只改開頭的時間，後面自己寫的字不動。
- **核對**：傳完後按「核對」，檢查影片是否都在 YouTube 上、大小和播放清單順序對不對。只讀取，不會改任何東西。
- **傳完再改**：已上傳的影片可以在程式裡改標題和說明，再一次送到 YouTube。
- **中斷可接續**：上傳中斷會自動重試，關掉程式後也能接著傳；碰到每日上限會自己等，隔天繼續。某一支失敗時，排在它前面的會傳完，後面的先停，下次再接著傳。
- 不需要另外安裝 ffprobe 或 exiftool，時間和 GPS 由程式直接解析 MP4。

## 為什麼要自己設定 Google Cloud

每個人都要用自己的 Google Cloud 專案，不能下載後直接登入。原因是所有人共用一個專案的話，要先通過 Google 的驗證，而且每日上傳上限也要大家一起分。你的憑證只存在自己的電腦，這個程式沒有伺服器。

## 第一次設定（約 10 分鐘，只要做一次）

### 1. 建立 Google Cloud 專案並啟用 API

1. 打開 <https://console.cloud.google.com/>，在左上角建立一個新專案，名稱隨意，例如 `gopro-uploader`。
2. 進入「API 和服務 → 程式庫」，搜尋 **YouTube Data API v3**，按「啟用」。

### 2. 設定 OAuth 同意畫面

1. 進入「API 和服務 → OAuth 同意畫面」（新版介面叫 Google Auth Platform）。
2. 使用者類型選「外部」，填寫應用程式名稱，以及你自己的 email。
3. 在「目標對象 / 測試使用者」加入你的 Gmail（就是 YouTube 頻道的帳號）。

> 專案停在「測試中」狀態時，登入大約 7 天就會過期，到時候程式會請你重新登入一次。
> 不想每週重登的話，可以在「目標對象」按「發布應用程式」。這只是改變你自己這個專案的狀態，不會把程式或影片公開給別人。之後登入時會出現「Google 尚未驗證這個應用程式」的警告，按「進階 → 繼續」即可。
> 按鈕不能按時，是「品牌」頁還缺網址：應用程式首頁填 `https://github.com/ZasonX/gopro-yt-uploader`，隱私權政策填 `https://github.com/ZasonX/gopro-yt-uploader/blob/main/PRIVACY.md`（這個程式的[隱私權政策](PRIVACY.md)），授權網域加 `github.com`，然後儲存。不要上傳標誌，上傳標誌就要送 Google 審查。

### 3. 建立用戶端憑證

1. 進入「API 和服務 → 憑證 → 建立憑證 → OAuth 用戶端 ID」。
2. 應用程式類型選 **電腦版應用程式**（Desktop app）。
3. 建立後按「下載 JSON」。

### 4. 在程式裡匯入

1. 到 [Releases](https://github.com/ZasonX/gopro-yt-uploader/releases) 下載最新版，不用另外安裝 Python。程式沒有付費的程式碼簽章，所以第一次開啟時系統會警告。
   - **Windows**（`…-windows.zip`）：解壓縮後雙擊 `GoProYTUploader` 資料夾裡的 `GoProYTUploader.exe`。SmartScreen 顯示「Windows 已保護您的電腦」時，按「其他資訊 → 仍要執行」。整個資料夾要放在一起，不能只把 exe 單獨複製出來。
   - **Mac**（`…-mac.zip`）：解壓縮後把 `GoProYTUploader.app` 拖到「應用程式」。第一次開啟被擋下時，到「系統設定 → 隱私權與安全性」，在下方按「強制打開」。
   - **Linux**（`…-linux.tar.gz`）：解壓縮後執行 `GoProYTUploader/GoProYTUploader`。如果視窗開不起來，可能要用套件管理員補裝 Qt 需要的系統函式庫（例如 Ubuntu 的 `libxcb-cursor0`）。
2. 按「設定 → 匯入 client_secret.json」，選剛剛下載的檔案。
3. 按右上角「登入」，在跳出的瀏覽器頁面登入 Google，並允許存取。

憑證、登入資訊和上傳紀錄只存在自己電腦的程式資料夾，不會進到 git：

| 系統 | 位置 |
|---|---|
| Windows | `%APPDATA%\GoProYTUploader` |
| Mac | `~/Library/Application Support/GoProYTUploader` |
| Linux | `~/.local/share/GoProYTUploader` |

## 平常使用

1. 「選擇…」影片資料夾，程式會自動掃描並列出每一段錄影。
2. 選擇播放清單，確認旅程時區、標題格式。
3. 需要地名的話，按「⋯ → 查詢地名」，把 GPS 座標換成地名。
4. 檢查表格：標題可以直接改；選取一列或多列後，可以在右側改時間和地點。
5. 按「開始上傳」。
6. 想設定地點的話，到 YouTube Studio 的「影片地點」貼上地名搜尋。

## 已知限制

- **Mac / Linux 版沒有實際用過**：見上方「支援的系統」。
- **登入約 7 天失效**：專案在「測試中」狀態時才有這個限制，改法見上方第 2 步。失效後按右上角「重新登入」，影片和上傳紀錄都不受影響。
- **每日上傳上限**：除了配額單位，YouTube 還限制每天能傳幾支，數字沒有公布。實際碰過的是頻道一天約 80 支、Cloud 專案一天 88 支。碰到時程式會等到隔天再繼續，電腦要開著；也可以按停止，之後再接著傳。
- **每日配額**：預設每天 10,000 單位。依 Google 2025-12 的修訂紀錄，每上傳一支影片約用 100 單位（以前是 1,600），請以 Cloud Console 的配額頁面為準。配額在太平洋時間午夜重置（台灣時間下午 3 點或 4 點）。
- **拍攝地點要到 Studio 設定**：YouTube 的 API 從 2017 年起就不接受寫入地點。程式查出的地名只是參考。Studio 的「影片地點」只能搜尋地名，不接受座標。
- **比對只看檔案大小**：比對是拿檔案的位元組大小和 YouTube 上的影片比，大小完全相同才算已上傳。合併過或重新輸出的影片大小不同，比不出來，要在比對結果裡自己看。API 也看不出影片是不是草稿，沒傳完的影片可能根本不會列出來。
- **拍攝日期改了不會自動更新**：已上傳的影片只會更新標題和說明，拍攝日期要到 Studio 改。
- **營利開關**：營利開關 API 改不了，但付費宣傳、為兒童打造這兩項都會設為「否」。

## 開發

需要 Python 3.10 以上版本。

```bash
run.bat                                        # Windows：從原始碼執行（第一次會建立 .venv 並安裝套件）
.venv\Scripts\python.exe -m pytest -q          # 測試（會自己產生假的 GoPro MP4）
.venv\Scripts\python.exe -m gopro_uploader --debug   # 開啟視窗並啟用開發者工具
build.bat                                      # Windows：打包成 dist\GoProYTUploader\GoProYTUploader.exe
python packaging/build.py                      # 任何系統：打包成該系統的版本（先裝 requirements-build.txt）
```

打包使用 PyInstaller，只能打包出執行它那個系統的版本，產生的是資料夾形式（Windows 約 116 MB，其中 ffmpeg 佔大部分）。圖示由 `packaging/make_icon.py` 產生，目前只有 Windows 版有圖示。

發布：推一個 `v` 開頭的標籤（例如 `git tag v0.2.0` 再 `git push origin v0.2.0`），GitHub Actions 會在 Windows、Mac、Linux 各跑一次測試並打包，再把成功的版本放到 Releases（`.github/workflows/release.yml`）。

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
