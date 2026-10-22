# Home Assistant・VOICEVOX検証環境

リポジトリのルートで次のコマンドを実行します。

```sh
docker compose -f compose.test.yaml up -d
```

Home AssistantとVOICEVOXエンジンをまとめて起動します。エンジンの応答を確認してからHome Assistantを起動します。

画面は http://127.0.0.1:8123 から開きます。初回は検証用アカウントを作成してください。エンジンのAPI操作画面は http://127.0.0.1:50021/docs です。

「設定」→「デバイスとサービス」→「統合を追加」から「VOICEVOX TTS」を追加し、次の接続先を指定します。

- ホスト: `voicevox-engine`
- ポート: `50021`

Home AssistantからはCompose内のサービス名でエンジンへ接続します。別途ホスト側でエンジンを起動する必要はありません。既存の検証用統合が`host.docker.internal`を使用している場合は、統合の「再設定」からホストを`voicevox-engine`へ変更してください。

## ファイルと変更の反映

- `custom_components/voicevox_tts`をコンテナに直接、読み取り専用でマウントします。
- Pythonコードを編集したら、次のコマンドで読み込み直します。
- 設定、アカウント、データベースはGit管理対象外の`test-config`へ保存します。
- 検証用の基本設定は`dev/homeassistant/configuration.yaml`です。
- Home Assistantは`stable`タグを使用します。通常の起動では取得済みイメージを使用し、開発開始時に下記の手順で明示的に更新します。
- テストに使用するエンジンのバージョンを確認し、必要に応じて`compose.test.yaml`のイメージタグとダイジェストを変更してから実行してください。
- 検証画面の公開先はDockerを実行するホストのループバックアドレスです。

```sh
docker compose -f compose.test.yaml restart homeassistant
```

## Home Assistantの更新

開発開始時に安定版の最新イメージを取得し、コンテナへ反映します。設定データと統合ソースのマウントは維持されます。

```sh
docker compose -f compose.test.yaml pull homeassistant
docker compose -f compose.test.yaml up -d
```

リリース前の検証では、使用したバージョンを次のコマンドで確認して検証結果に記録してください。

```sh
docker compose -f compose.test.yaml exec -T homeassistant python -m homeassistant --version
```

## ログと停止

```sh
docker compose -f compose.test.yaml logs -f --tail=100 homeassistant
docker compose -f compose.test.yaml logs -f --tail=100 voicevox-engine
docker compose -f compose.test.yaml down
```

停止後も`test-config`のデータは残ります。音声生成の比較ではTTSキャッシュを無効にし、同じ文章と声で試してください。

この構成はDockerとDocker Composeを使用した統合開発用です。実際の音声端末や別端末からの接続試験では、Home Assistantの公開アドレスやネットワーク構成を別途調整してください。
