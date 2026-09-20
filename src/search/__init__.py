"""检索层（Layer 4）：类百度检索 + 三级缓存 + 有倾向性采集。

对外只暴露一个门面：``service.build_search_service(cfg)``。
Web 层（``src.search.server``）不允许绕过它直接碰数据库或索引。
"""
