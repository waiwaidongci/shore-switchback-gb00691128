初始结构由 `src/models/tables.py` 定义，在 API 启动前由 `metadata.create_all` 创建。演示数据通过独立入口填充。

补报相关表（`revision_meta`、`base_versions`、`supplement_links`、`merge_reports`、`approval_pins`）为增量新增，`create_all` 会在既有库上补建。启动时 `src.services.revisions.bootstrap` 会把历史数据版本登记为各自基底，并把每个基底最新一次已完成分析设为当前生效运行，保证升级后旧结果仍可见。
