# Memory Contract v1（实现约束）

## 1. 类型（memory_type）

允许值：

- `feedback`：高优先级硬规则/复盘结论
- `constraint`：边界与约束
- `preference`：用户偏好
- `fact`：稳定事实
- `general`：通用记忆
- `working`：短期工作记忆（默认短 TTL）

非法值统一回落为 `general`。

---

## 2. 生命周期默认值

默认过期策略（可被显式 `expires_at` 覆盖）：

- `working`：7 天
- `general`：180 天
- `feedback` / `constraint` / `preference` / `fact`：默认不过期

每次读取会更新：

- `access_count += 1`
- `last_accessed = now`

读取时自动过滤过期记录。

---

## 3. 注入优先顺序

记忆注入遵循：

1. `feedback`
2. `constraint`
3. `preference`
4. `fact`
5. `general` / `working`

当前实现中 `feedback` 会前置并做显式标记。

---

## 4. 清洗规则

写入前统一清洗：

- `key`: 去首尾空格，最大 128 字符
- `value`: 去首尾空格，最大 4000 字符
- `memory_type`: 统一 lower + 枚举校验

空 `key` 直接拒绝写入。

---

## 5. API 与调用约定

- `save_memory` 工具写入时必须走 `normalize_memory_type`。
- `merge_global_fact` 与 consolidation 管道均使用同一类型规范。
- 未来如扩展 `confidence/source/priority`，需先升级 contract，再升级存储与服务层。

---

## 6. 冲突策略（v1.1）

- `preference` 冲突：默认不直接覆盖全局值，写入条件化键  
  例如：`<key>__context__<session_id>`。
- 覆盖前版本化：若旧值与新值不同，保留  
  `"<key>__version__<timestamp>"` 便于追溯与回滚。
- 其他类型冲突：仍走 LLM 冲突消解，但最终落库前也会补版本快照。

---

## 7. 质量看板与健康检查

- 质量指标：
  - `conflict_rate`
  - `mis_hit_rate`
  - `inject_budget_chars_utilization`
  - `inject_budget_items_utilization`
- 诊断接口：`GET /memory/health?session_id=...`
  - 返回存储分布（总量、过期量、分类分布）
  - 返回指标看板与原始计数器

