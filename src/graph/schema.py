"""
schema.py
Neo4j 图 Schema 初始化
创建约束、索引，定义节点/边类型
"""

from __future__ import annotations

# 建立约束和索引的 Cypher 语句
SCHEMA_STATEMENTS = [
    # ── 约束（唯一性）──────────────────────────────────────────
    "CREATE CONSTRAINT func_id IF NOT EXISTS FOR (f:Function) REQUIRE f.id IS UNIQUE",
    "CREATE CONSTRAINT class_id IF NOT EXISTS FOR (c:Class) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT file_id IF NOT EXISTS FOR (f:File) REQUIRE f.id IS UNIQUE",
    "CREATE CONSTRAINT process_id IF NOT EXISTS FOR (p:Process) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT community_id IF NOT EXISTS FOR (c:Community) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT meta_id IF NOT EXISTS FOR (m:Meta) REQUIRE m.id IS UNIQUE",

    # ── 查询索引 ──────────────────────────────────────────────
    "CREATE INDEX func_name IF NOT EXISTS FOR (f:Function) ON (f.name)",
    "CREATE INDEX func_qualified IF NOT EXISTS FOR (f:Function) ON (f.qualifiedName)",
    "CREATE INDEX func_domain IF NOT EXISTS FOR (f:Function) ON (f.domain)",
    "CREATE INDEX func_file IF NOT EXISTS FOR (f:Function) ON (f.filePath)",
    "CREATE INDEX func_branch IF NOT EXISTS FOR (f:Function) ON (f.branch)",
    "CREATE INDEX class_name IF NOT EXISTS FOR (c:Class) ON (c.name)",
    "CREATE INDEX class_branch IF NOT EXISTS FOR (c:Class) ON (c.branch)",
    "CREATE INDEX file_path IF NOT EXISTS FOR (f:File) ON (f.path)",
    "CREATE INDEX community_domain IF NOT EXISTS FOR (c:Community) ON (c.domain)",

    # ── Vector index (Neo4j 5.6+, for embedding cosine search) ──────
    """CREATE VECTOR INDEX func_embedding IF NOT EXISTS
    FOR (f:Function) ON (f.embedding)
    OPTIONS {indexConfig: {
      `vector.dimensions`: 1024,
      `vector.similarity_function`: 'cosine'
    }}""",
]

# Meta 节点：存储图的版本元数据
META_NODE_TEMPLATE = {
    "id": "meta:base",
    "lastUpdated": None,       # ISO 8601 时间戳
    "commitHash": None,        # master HEAD commit
    "totalFiles": 0,
    "totalFunctions": 0,
    "totalClasses": 0,
    "totalEdges": 0,
    "coveragePercent": 0.0,
    "parseErrors": [],
}


def init_schema(driver) -> None:
    """在 Neo4j 中初始化 schema（幂等，可重复执行）"""
    with driver.session() as session:
        for stmt in SCHEMA_STATEMENTS:
            try:
                session.run(stmt)
            except Exception as e:
                # 约束已存在时忽略
                if "already exists" not in str(e).lower():
                    raise
        # 初始化 Meta 节点
        session.run("""
            MERGE (m:Meta {id: 'meta:base'})
            ON CREATE SET m += $props
        """, props=META_NODE_TEMPLATE)


def update_meta(driver, **kwargs) -> None:
    """更新 Meta 节点中的统计字段"""
    with driver.session() as session:
        set_clauses = ", ".join(f"m.{k} = ${k}" for k in kwargs)
        session.run(f"""
            MERGE (m:Meta {{id: 'meta:base'}})
            SET {set_clauses}
        """, **kwargs)
