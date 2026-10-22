# AGENTS

## プロジェクト

VOICEVOXエンジンを利用するHome AssistantのカスタムTTS統合です。

- `custom_components/voicevox_tts/`: 統合本体。`tts.py`が音声生成、`config_flow.py`が設定画面、`__init__.py`がセットアップを担当します。
- `custom_components/voicevox_tts/manifest.json`: 統合のバージョンとメタデータ。
- `custom_components/voicevox_tts/strings.json`、`translations/`: 設定画面の文言と翻訳。
- `README.md`、`docs/README-ja.md`: 利用者向けの英語・日本語ドキュメント。
- `compose.test.yaml`、`dev/homeassistant/`: Docker Composeによる検証環境と操作手順。
- `test-config/`: 検証環境の設定や認証情報などの実行時データ。Git管理対象外です。

## ログ出力

- ログには`print`ではなく、`logging.getLogger(__name__)`で取得したロガーを使用してください。
- ログメッセージは英語で記述してください。
- 既存のログは残し、処理を共通化する場合も対応するタイミングで出力してください。

## docstring

- モジュール、クラス、関数のdocstringは英語で記述してください。

## 検証環境

テストが必要だと言われた場合は、以下をリポジトリのルートで実行します。

```sh
docker compose -f compose.test.yaml up -d
```

- Home Assistant: `http://127.0.0.1:8123`
- VOICEVOX API操作画面: `http://127.0.0.1:50021/docs`
- 統合に設定するエンジンの接続先: ホスト`voicevox-engine`、ポート`50021`

統合フォルダはコンテナの`/config/custom_components/voicevox_tts`へ読み取り専用で直接マウントしています。ファイルをコピーせず、リポジトリ内のソースを編集してください。Pythonコードの変更後はHome Assistantを再起動します。

```sh
docker compose -f compose.test.yaml restart homeassistant
docker compose -f compose.test.yaml logs --tail=100 homeassistant
docker compose -f compose.test.yaml logs --tail=100 voicevox-engine
```

Home Assistantは`stable`タグを使用します。更新が必要な場合は明示的に取得して反映します。

```sh
docker compose -f compose.test.yaml pull homeassistant
docker compose -f compose.test.yaml up -d
```

エンジンは`compose.test.yaml`のイメージタグとダイジェストで指定します。テスト対象のバージョンを確認し、変更する場合は両方を更新してください。別途ホスト上でエンジンを起動する必要はありません。

停止には次のコマンドを使用します。`test-config`の設定データは残ります。

```sh
docker compose -f compose.test.yaml down
```

検証用アカウントや統合の設定は保持してください。`test-config`の認証情報を出力したり、Git管理対象に追加したりしないでください。接続先などの設定変更は、原則としてHome Assistantの画面から行ってください。

詳しい操作手順は`dev/homeassistant/README.md`を参照してください。
