# 自适应中文能力诊断后端

面向东盟职业院校中文培训中心的自适应测评后端：维护可版本化的能力图谱、
题库与评分规则，在一次测评会话内根据已确认作答选择下一题，支持断网续考、
教师按证据作废异常作答、受控重评，并让分数与等级永久绑定当时的规则版本。

## 设计要点

### 版本化内容（`models.py` / `storage.py`）

* **能力图谱** `SkillGraphVersion(revision)` + `SkillGraph(prerequisites)`：
  先修关系在登记时校验“引用存在 + 无环”；支持祖先闭包与后继传播查询。
* **题库** `ItemBankVersion(revision)` + `Item`：题目不可变，题面/答案/
  难度/技能归属/曝光上限随修订固化；题目可在新修订中停用（`active=False`），
  旧修订永久保留。
* **评分规则** `ScoringRuleSet(rule_set_id, revision)`：等级表、阈值、
  掌握/缺口确认次数、收敛阈值、题量上下界均为版本化数据。
* 同一实体的同一修订号重复登记返回 409（`VersionConflictError`）。

### 会话与版本固化（`services.py`）

* 开考时**显式钉住**图谱/题库/规则三个修订号（规则修订号必填，禁止
  默认漂移）；会话 JSON 与结果快照都记录这三个 key。
- 结束后产生不可变 `SessionResult`；题库后来发布 rev2、下线题目，都不
  改写旧结果。受控重评只按钉住版本与当前有效证据重算。

### 自适应选题（`selector.py`，纯函数、零随机数）

1. **覆盖优先**：证据最少的技能先测，高水平学员不会反复见同类题；
2. **先修约束**：先修技能确认缺口后，后继技能本轮阻塞；已确认缺口的
   技能本身也停止出题，避免初学者连续受挫；
3. **难度匹配**：题难度最接近该技能当前 θ，等距偏难半档优先；
4. **曝光约束**：本会话已呈现、停用、全库曝光用尽的题一律排除。

所有并列都按稳定键裁决（技能按拓扑序与标识，题目按难度与标识），
因此自动化场景对相同输入永远复现同一选择。

### 能力估计与缺口传播（`scoring.py`）

确定性 Rasch 型均值/方差递推（无随机数，可逐题复算）。技能出现规定次数
“有把握的失败”即确认缺口，并沿**先修 → 后继**边传播：后继即使答对较多
也暂不确认掌握，`gap_propagated_from` 记录来源，方便教师解释。

### 幂等与断网续考

* 所有写操作携带 `request_id`；同标识重试返回首次响应。
* 选题成功即生成 PENDING 证据与一次性 `token`；断网后**必须凭原
  request_id 续考**，换新标识会收到 409 `pending_selection`（带回原
  request_id、token、item_id）。
* **曝光占用按 `(会话, request_id)` 幂等记账**（`exposures.json` 的
  reservations）：台账已扣减而进程崩溃时，重试复用原占用，绝不重复扣减。
* 作答携带选题 token；作答的崩溃窗口由证据上的 `answer_request_id` 恢复。

### 教师作废与受控重评

* `void_evidence` 必须登记教师工号与理由；进行中的证据转 VOIDED 后
  不再参与评分与选题，曝光不退还（防止借作废绕过曝光控制）。
* 已结束会话作废即触发受控重评；`regrade` 支持申诉重算。原结果完整
  进入 `result_history`，审计日志记录等级/θ 的前后变化与所用版本。

### 可解释性

`GET /sessions/{id}/explain` 返回：每题的 `selection_reason`（为什么选它）、
`contribution`（该题如何改变技能 θ 与不确定性）、状态/作废信息、逐技能
掌握与缺口（含传播来源）、结果的 `level_basis`（θ 落在哪个阈值区间）以及
完整审计日志。

## HTTP 接口

标准库实现，无第三方依赖（`api.py`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/graphs` `/admin/banks` `/admin/rules` | 登记版本化内容 |
| POST | `/sessions` | 开考（body 钉住各修订号） |
| POST | `/sessions/{id}/next` | 下一题（`request_id`，幂等） |
| POST | `/sessions/{id}/answer` | 提交作答（`request_id`+`token`） |
| POST | `/sessions/{id}/finish` | 交卷 |
| POST | `/sessions/{id}/void` | 教师作废（教师工号+理由） |
| POST | `/sessions/{id}/regrade` | 受控重评/申诉重算 |
| GET  | `/sessions/{id}` | 会话状态与结果 |
| GET  | `/sessions/{id}/explain` | 逐题依据、贡献、审计 |
| GET  | `/healthz` | 健康检查 |

错误以 JSON 返回：400 校验错误、404 不存在、409 幂等冲突/会话已结束/
存在待作答题（`pending_selection` 带续考字段）、422 其余领域错误。

## 运行

需要 Python 3.11+（仅标准库）。

```bash
# 端到端演示（临时目录，打印选题/续考/作废/重评全过程）
python3 run_cli.py demo

# 播种演示内容并启动 HTTP 服务
python3 run_cli.py seed  --root data
python3 run_cli.py serve --root data --port 8080
```

测试与编译检查：

```bash
python3 -m unittest discover -s tests -v   # 65 个用例
python3 -m compileall -q src tests run_cli.py
```

## 代码结构

```
src/adaptive_exam/
  models.py    版本化图谱/题库/规则、证据、会话、结果快照（不可变值对象）
  scoring.py   确定性能力估计、缺口传播、定级与单题贡献解释
  selector.py  自适应选题（纯函数、稳定破并）、终止判定
  services.py  用例编排：幂等、续考、曝光、作废、受控重评、解释
  storage.py   原子 JSON 持久化：内容登记处、曝光台账、会话与幂等记录
  codecs.py    全部领域对象的 JSON 编解码（重启恢复）
  api.py       标准库 HTTP 适配层
  fixtures.py  可复现的东盟职业中文迷你内容包与固定时钟
  errors.py    领域错误类型
```

## 持久化与并发说明

所有写盘均为“临时文件 + `os.replace`”原子替换；服务层以进程内
`threading.RLock` 保证临界区，HTTP 层为多线程服务。同一数据目录设计为
单进程访问；多实例部署时应把 `storage.py` 的三个仓储替换为带唯一约束/
事务的数据库实现（领域与服务层接口不变）。
