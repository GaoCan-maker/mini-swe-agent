# AST-Shell 设计文档

> 状态：设计稿 v1（未实现）
> 适用代码基线：`mini-swe-agent` 的 `main` 分支（原始版本，`__version__ = "2.4.6"`）
> 评测环境：WSL Ubuntu-22.04，conda 环境 `deepswe`，通过 DeepSWE（Harbor）运行

---

## 目录

0. 一句话概括与术语
1. 背景与动机（含 DeepSWE 轨迹统计）
2. 设计目标、非目标与不变量
3. 运行环境与集成点
4. 总体架构与数据流
5. 命令解析与改写（含标记协议）
6. 符号索引（Tree-sitter）与解析缓存
7. 输出渲染格式规范
8. 算子 O1：SnapRead（区间读取的边界对齐）
9. 算子 O2：AutoSkeleton（整文件读取的骨架化）
10. 算子 O3：SmartGrep（搜索结果的符号标注与重排）
11. 算子 O4：SeenFold（跨轮次已读未改符号折叠）
12. 输出预算守卫
13. 会话状态
14. 放行规则与边界情况
15. 配置项全表
16. 日志与可观测性
17. 代码结构与接口
18. 测试计划
19. 实验方案
20. 风险与开放问题
附录 A：端到端示例
附录 B：DeepSWE 轨迹统计汇总与口径

---

## 0. 一句话概括与术语

**一句话**：AST-Shell 不给 Agent 增加任何新工具、不修改任何提示词。它在 bash 命令的执行层，透明地把 Agent 原本就在用的 `sed -n` / `nl -ba | sed -n` / `cat` / `head` / `grep` / `rg` 的输出，改写成"与语法树符号边界对齐"的输出，从而减少多余代码、翻页式重读和搜索循环。

| 术语 | 含义 |
|---|---|
| action | Agent 一次 tool call 中的一条 bash 命令字符串（`action["command"]`）。一轮（turn）可以有多个 action |
| 子命令（segment） | 一条 action 按顶层 `&&`、`\|\|`、`;`、换行拆分后的片段。一个子命令可以是一个管道（pipeline） |
| 读取子命令 | 被识别为"只读地输出某个源码文件的部分或全部行"的子命令（见 5.3） |
| 搜索子命令 | 被识别为 `grep` / `rg` / `git grep` 的子命令（见 5.3） |
| 符号（symbol） | Tree-sitter 识别出的函数、方法、类、结构体、接口、类型定义等（见 6.2） |
| 区间读取 | 读取文件第 `X` 到第 `Y` 行（`X > 1`，或覆盖不足文件的 80%） |
| 整文件式读取 | `cat f`、`nl -ba f`，或从文件头开始、覆盖 ≥ 80% 文件的读取（见 9.1） |
| 原样输出 | 原始命令在容器里执行得到的输出，逐字节不变 |
| 渲染块（block） | 对一个读取/搜索子命令，由 AST-Shell 生成、用来替换其原样输出的文本 |
| 占位行 | AST-Shell 插入的说明行，固定格式，见第 7 节 |
| 观察截断阈值 | observation 模板中的 10000 字符阈值。超过时只保留头 5000 + 尾 5000 字符。`main` 分支的 `default.yaml`/`swebench.yaml` 和 DeepSWE（Harbor）轨迹中实测都是这一规则 |

---

## 1. 背景与动机

### 1.1 问题

Coding Agent 探索代码库靠的是行级文本工具。这些工具不知道程序结构，导致三类浪费：

1. **窗口不对齐**：Agent 用 `sed -n 'X,Yp'` 猜一个窗口。窗口开头常带着上一个函数的尾巴，中间夹着无关函数，结尾又把目标函数切断，迫使 Agent 下一轮继续翻页。
2. **整文件倾倒**：Agent 用 `cat` 或 `sed -n '1,320p'` 一次读入大文件，只是为了"看看里面有什么"，大量函数体进入上下文后再也没用到。输出一旦超过 10000 字符，中间部分还会被截掉，Agent 只能再读一次。
3. **搜索循环**：`grep` 只返回"文件:行号:内容"，不告诉 Agent 这一行属于哪个函数、函数从哪到哪。Agent 只能再用 `sed` 开窗口去看。另外 `grep | head -50` 按文件名字母顺序截断，测试文件常常挤掉真正的定义。

由于对话历史只增不减，早期读进来的多余代码会在后续每一轮都被重复计费。

### 1.2 与 SWE LSP、Serena 的区别

| | Serena | SWE LSP | AST-Shell（本方案） |
|---|---|---|---|
| 分析后端 | 语言服务器（LSP） | 语言服务器（复用 Serena 的 `solidlsp`） | Tree-sitter（纯语法，容错，无需项目依赖） |
| 新增工具 | 大量（含符号级编辑） | 3 个（`get_code_overview`、`read_def`、`find_refs`） | **0 个** |
| 改变的对象 | 给 Agent 新动作 | 新动作 + grep/traceback 输出增强 | **只改变已有命令的输出** |
| 对 `sed -n` 窗口 | 不处理 | 不处理（期望 Agent 改用 `read_def`） | 自动对齐到符号边界、折叠残尾 |
| 对整文件读取 | 不处理 | 不处理 | 骨架化 |
| 跨轮次重复读 | 不处理 | 不处理 | 按符号内容哈希折叠 |
| 环境依赖 | 需能启动各语言 LSP | 同左；最坏情况全量索引 584 秒 | 宿主进程内解析，毫秒级 |

SWE LSP 的消融实验（其 Table 3）说明：只提供导航工具而不改造已有输出，token 反而会上升。本方案沿着这个结论再往前走一步：**完全不引入新动作**，把结构信息直接嵌进 Agent 已经在用的命令输出里，并给出精确行号区间，让 Agent 下一步用自己习惯的 `sed -n 'a,bp'` 就能一次读全目标函数。

### 1.3 DeepSWE 轨迹中的证据（详见附录 B）

- 单次 observation 的字符数中，`cat/head/tail/nl` 所在 action 占 58.0%，`sed -n` 占 48.3%，`grep/rg` 占 24.9%（复合命令会被重复计入多个类别，所以各项之和超过 100%）。按"剩余轮数加权"（越早读入、被重复计费越多）估算，这三类合计占累计计费上下文的 75% 以上；测试运行只占 3.9%。
- `sed -n 'X,Yp'` 区间读取中，54.9%（1924/3505）读的是本轨迹之前读过的文件，33.8%（1186 次）与之前读过的区间有重叠。
- 整文件 `cat` 中有 39.1%（752/1920）是同一轨迹内的重复读取。
- `grep/rg` 中有 54.2%（2438/4500）带 `| head`。
- 70.0% 的轮次使用了 `&&` 或 `;` 串起来的复合命令。
- 8.5% 的 observation 超过 10000 字符被截断。
- `sed -n` 窗口大小的中位数为 70 行。
- 100 条轨迹中有 521 次用 `grep -n "def \|func \|class "` 手工拼文件目录。
- 涉及的语言以 Python、TypeScript、Go 为主，其次是 Rust、JavaScript，C/C++ 很少。

---

## 2. 设计目标、非目标与不变量

### 2.1 目标

- G1：减少探索阶段进入上下文的 token（observation 字符数）。
- G2：减少探索轮数（尤其是翻页式重读和因截断导致的重读）。
- G3：不降低任务解决率。
- G4：对 Agent 零学习成本，不新增工具，不修改提示词。
- G5：与语言、运行环境（本地 / Docker / Harbor）解耦。

### 2.2 非目标

- 不做语义级分析（类型推断、跨文件跳转定义、引用查找）。这些属于 LSP 的范畴，不在 v1 范围内。
- 不修改历史消息（不做回溯式压缩），保证提示缓存前缀不被破坏。
- 不处理编辑命令、测试输出、git 输出。
- 不改变命令的副作用和退出码。

### 2.3 不变量（实现时必须逐条保证，并有对应测试）

- **I1 行号真实性**：渲染块中每一个非占位行都能确定它在文件中的真实行号 n，且内容与执行那一刻文件第 n 行逐字符相同。带行号格式下 n 直接写在行首（`%6d\t<text>`）；锚点格式下 n 由起点和占位行里的 `L{a}-{b}` 推出（见 7.1）。渲染完成后要做校验，校验不通过就回退（见 7.4）。
- **I2 不改则不动**：没有任何算子触发时，输出与原样输出逐字节一致。
- **I3 退出码不变**：改写后命令的退出码与原命令一致。
- **I4 副作用不变**：除了额外读取文件以外，不新增、删除或重排任何有副作用的子命令。
- **I5 失败即回退**：任何解析失败、超时、异常、未知格式，都回退为原样输出，并在日志里记录原因。AST-Shell 自身绝不能让一条命令失败。
- **I6 提交保护**：任何包含 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 的 action 完全不改写。
- **I7 内容快照一致**：渲染所用的文件内容，必须是该读取子命令执行那一刻的内容（见 5.4），不能是缓存里的旧内容。
- **I8 不碰历史**：只改写当前 action 的输出，不修改任何已发送的消息。

---

## 3. 运行环境与集成点

### 3.1 DeepSWE（Harbor）的运行方式（已确认）

在 WSL `deepswe` 环境中检查了 Harbor 0.24.0 的适配代码 `harbor/agents/installed/mini_swe_agent.py`，结论如下：

- **只有容器内模式**。Harbor 在任务容器里用 `uv tool install --python 3.12 mini-swe-agent{==version} --with litellm --with orjson --with fastapi` 从 PyPI 安装 mini-swe-agent，然后在容器里执行：
  ```
  mini-swe-agent --yolo --model=... --task=... --output=/logs/agent/mini-swe-agent.trajectory.json -c mini [-c /tmp/mswea-config/custom.yaml] -c ... --exit-immediately
  ```
  Harbor 0.24.0 中没有 `mini-swe-agent-host`。轨迹里那个名字来自爬取方用的另一套框架，与本地评测无关。
- **入口是 `run/mini.py`**：
  - Environment 用 `get_environment(..., default_type="local")`。`mini.yaml` 没有指定 `environment_class`，所以用的是容器内的 `LocalEnvironment`，命令通过 `subprocess` 交给 bash 执行；
  - Agent 用 `get_agent(..., default_type="interactive")`，即 **`InteractiveAgent`**（`--yolo` 时跳过确认）。配置里的 `agent.agent_class` 可以替换它；
  - 配置 = `mini.yaml` 加上 Harbor 追加的 `-c` 项。Harbor 的 `MiniSweAgent` 接受 `config`（字典）或 `config_file` 参数，内容会写到容器的 `/tmp/mswea-config/custom.yaml`，再通过 `-c` 合并进去。所以 `agent.agent_class` 和 AST-Shell 的配置都能经这个通道传入。
- **命令全部经过 `env.execute(action)`**：`InteractiveAgent.execute_actions` 重写了父类的方法，但它内部同样是 `for action in actions: outputs.append(self.env.execute(action))`。
- **observation 模板**：`mini.yaml` 的模板以 10000 字符为阈值，超过时输出 `output_head`（前 5000）、`output_tail`（后 5000）、`elided_chars`，与 DeepSWE 轨迹中的格式完全一致。
- **Harbor 支持自定义 Agent 类**：`AgentFactory` 接受 `module.path:ClassName` 形式的 import path（`--agent` 或 `import_path`）。
- **Tree-sitter 要装在任务容器里**，与 mini-swe-agent 装在同一个 uv tool 环境中。

### 3.2 设计上的关键选择：核心与运行环境无关

AST-Shell 的核心只依赖一个函数：

```python
execute: Callable[[dict], dict]   # 输入 {"command": str}，返回 {"output": str, "returncode": int, ...}
```

文件内容不通过额外通道读取，而是通过**改写后的同一条命令**在执行环境里顺带输出（见 5.4）。这样一来：
- 不需要单独的"读文件接口"，在 Local、Docker、Harbor 中都一样工作；
- 读到的内容天然是执行那一刻的快照（满足 I7）；
- 不需要在宿主机追踪 `cd` 和相对路径，路径由容器里的 `realpath` 和 `$PWD` 给出。

唯一的额外调用出现在 SmartGrep：需要读取命中文件的内容时，会再发起一次不计入 Agent 步数的 `execute`（见 10.4）。

### 3.3 集成方式（已确定：Agent 层，通过包装 env 实现）

不能只重写 `DefaultAgent.execute_actions`：Harbor 下实际运行的是 `InteractiveAgent`，它有自己的 `execute_actions`（含确认、`Submitted` 处理、`try/finally` 保留部分输出）。重写会丢掉这些行为，或者需要复制它的代码。

做法：新增 `ASTShellAgent(InteractiveAgent)`，**不重写 `execute_actions`**，只在 `__init__` 中把 `self.env` 换成一个薄代理。代理的 `execute` 走 AST-Shell，其他属性全部转发给原 env：

```python
class ASTShellAgentConfig(InteractiveAgentConfig):
    ast_shell: ASTShellConfig = ASTShellConfig()


class _ASTShellEnv:
    def __init__(self, env: Environment, agent: "ASTShellAgent"):
        self._env, self._agent = env, agent

    def execute(self, action: dict, cwd: str = "") -> dict:
        return self._agent.ast_shell.run(action, lambda a: self._env.execute(a, cwd), turn=self._agent.n_calls)

    def __getattr__(self, name: str):
        return getattr(self._env, name)


class ASTShellAgent(InteractiveAgent):
    def __init__(self, model: Model, env: Environment, **kwargs):
        super().__init__(model, _ASTShellEnv(env, self), config_class=ASTShellAgentConfig, **kwargs)
        self.ast_shell = ASTShell(self.config.ast_shell)

    def run(self, task: str = "", **kwargs) -> dict:
        self.ast_shell.reset(task=task)
        return super().run(task, **kwargs)
```

这样做的好处：
- `InteractiveAgent` 和 `DefaultAgent` 的执行循环都不用动，确认逻辑、提交逻辑、异常处理照旧；
- 能拿到任务文本（`run` 的参数，用于 9.2 的焦点判断）和轮数（`n_calls`，用于 M-SEEN 的 `{d}`）；
- 与 Environment 类无关：Local、Docker 等都能用；
- `_check_finished` 仍在原 env 的 `execute` 中触发，提交动作被 AST-Shell 放行（I6），`Submitted` 异常照常向上传递；
- 只需在配置里写 `agent.agent_class: minisweagent.agents.astshell.ASTShellAgent`，`get_agent` 已经支持。

在 `run/benchmarks/swebench.py` 中使用时（它硬编码了 `ProgressTrackingAgent`），可以用同样的代理方式写一个 `ProgressTrackingAgent` 的子类。v1 只做 Harbor 评测，这一项可以推迟。

### 3.3.1 在 Harbor 中的接入步骤

1. **自定义 Harbor Agent**：在仓库中新增 `harbor_ext/astshell_mini.py`（不放进 `minisweagent` 包，以免引入对 harbor 的依赖），定义 `ASTShellMiniSweAgent(MiniSweAgent)`，只重写 `install()`：
   - 用 `environment.upload_dir()` 把本仓库（或预先构建好的 wheel）上传到容器的 `/tmp/mswea-src`；
   - 把安装命令换成 `uv tool install --python 3.12 /tmp/mswea-src --with litellm --with orjson --with fastapi --with tree-sitter --with tree-sitter-language-pack`；其余步骤（uv 的安装、PATH 设置）照抄父类。
2. **运行命令**：`--agent harbor_ext.astshell_mini:ASTShellMiniSweAgent`（运行时 `PYTHONPATH` 要包含仓库根目录），通过 Agent 参数传入 `config`：
   ```yaml
   agent:
     agent_class: minisweagent.agents.astshell.ASTShellAgent
     ast_shell:
       enabled: true
   ```
3. **Baseline 也必须用这个自定义 Agent 安装同一份代码**，只把 `ast_shell.enabled` 设为 `false`。否则 Baseline 用的是 PyPI 上的版本，两组的 mini-swe-agent 版本不同，对比就不公平了。

### 3.3.2 尚未验证、需要实际跑一次确认的地方

- `upload_dir` 在 DeepSWE 使用的环境后端（Docker 或远程沙箱）上是否可用、速度如何。不行的话，改为先构建 wheel，用 `upload_file` 上传单个文件。
- `tree-sitter-language-pack` 在任务镜像上能否装上：manylinux 镜像有预编译 wheel；musl（Alpine）镜像可能没有，需要现场编译。可以先在几个镜像上试装并统计。
- `config` 参数传入后，`-c mini` 和 `custom.yaml` 的合并结果是否包含 `agent.agent_class`。可以在一次运行结束后，检查轨迹中 `info.config.agent` 的内容。

### 3.4 依赖

```
tree-sitter >= 0.23
tree-sitter-language-pack      # 预编译语法，get_parser("python") 即可
```

放在 `pyproject.toml` 的可选依赖组 `ast_shell` 中。不安装时功能不可用；配置开启却缺依赖时，在初始化阶段直接报错，不要静默降级。

---

## 4. 总体架构与数据流

```
Agent 产生 action
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ ASTShell.run(action, execute)                              │
│                                                           │
│ 1. 放行检查（第 14 节） ──命中──► 直接 execute(action)      │
│ 2. 解析：拆分子命令，识别读取/搜索子命令（5.2、5.3）        │
│    未识别到任何读取/搜索子命令 ──► 直接 execute(action)     │
│ 3. 改写：给每个被识别的子命令套上标记块（5.4）              │
│ 4. execute(改写后的命令)  ← 只执行一次                      │
│ 5. 拆解输出：切出各标记块（文件快照、PWD、原样输出、退出码）│
│ 6. 逐块渲染：                                               │
│      读取块 → 解析缓存取符号表 → O1 / O2 / O4               │
│      搜索块 →（必要时再取命中文件）→ O3                     │
│ 7. 预算守卫：总长超限则逐块降级（第 12 节）                 │
│ 8. 拼接：渲染块替换原样块，其余输出原封不动                 │
│ 9. 不变量校验（I1）；任何块失败都只回退该块                 │
│ 10. 更新会话状态（第 13 节），写日志到 output["extra"]       │
└───────────────────────────────────────────────────────────┘
        │
        ▼
observation 模板（10000 字符截断仍然存在，作为最后一道保险）
```

**回退粒度是"块"**：一个块渲染失败，只把该块恢复成原样输出，其余块照常渲染。

---

## 5. 命令解析与改写

### 5.1 总原则

- 只做"保守识别"：识别不了就不动。
- 被识别的子命令**原样保留并照常执行**，改写只是在它外面包一层标记，并额外快照文件内容。
- 未被识别的子命令连一个字符都不改。

### 5.2 顶层拆分器 `split_top_level(cmd) -> list[Segment] | None`

逐字符扫描，维护以下状态：
- 单引号、双引号内；
- 反斜杠转义；
- `$(...)` 嵌套深度、反引号；
- `(...)` 子 shell 深度、`{ ...; }` 组深度；
- here-doc：遇到 `<<` / `<<-` 后读取定界符（可能带引号），从下一行开始一直跳到"整行等于定界符"的行。

切分规则：
- 只在所有深度为 0、且不在引号或 here-doc 中时，按 `&&`、`||`、`;`、换行切分。记录每个片段的起止下标和后面跟着的分隔符，以便原样拼回。
- 在每个片段内部，再按顶层 `|`（不是 `||`）切出管道阶段。
- 出现 `for`、`while`、`until`、`if`、`case`、`function`、`select` 关键字的片段，标记为"复合语句"，整体不识别（但不影响其他片段）。
- 片段末尾的后台符 `&` → 该片段不识别。
- 扫描出错（引号不闭合、here-doc 定界符找不到）→ 返回 `None`，整条 action 放行。

**正确性要求**：把所有片段按记录的下标原样拼回，必须等于原命令。这一点要写成测试。

### 5.3 子命令识别

每个片段用 `shlex.split` 切词（失败则不识别）。允许的前后缀如下：

- 允许的前缀：无（`cd`、环境变量赋值是独立的片段，不影响识别）。
- 允许的后缀重定向：`2>/dev/null`、`2>&1`。出现任何其他重定向（`>`、`>>`、`<`、`&>`、`tee`）都不识别。
- 文件路径参数必须匹配 `^[A-Za-z0-9_./@+~-][A-Za-z0-9_./@+-]*$`，且不含 `$`、`*`、`?`、`[`、`{`、反引号；路径不能以 `~` 开头（避免展开语义差异）。不满足则不识别。

#### 5.3.1 读取子命令（ReadSpec）

识别结果：`ReadSpec(path, ranges: list[(X, Y)], numbered: bool, form: str)`，其中 `Y = None` 表示读到文件末尾。

| 形式 | 示例 | ranges | numbered |
|---|---|---|---|
| R1 | `sed -n 'X,Yp' f` | `[(X,Y)]` | 否 |
| R2 | `sed -n 'X,Yp;A,Bp' f`（多段） | `[(X,Y),(A,B)]` | 否 |
| R3 | `sed -n 'Xp' f` | `[(X,X)]` | 否 |
| R4 | `nl -ba f \| sed -n 'X,Yp'`（可多段） | 同上 | 是 |
| R5 | `cat -n f \| sed -n 'X,Yp'` | 同上 | 是 |
| R6 | `cat f \| sed -n 'X,Yp'` | 同上 | 否 |
| R7 | `awk 'NR>=X && NR<=Y' f`（也接受 `NR>=X&&NR<=Y{print}`、`{print NR":"$0}`） | `[(X,Y)]` | `print NR...` 时为特殊格式，**v1 不识别带 NR 的打印形式** |
| R8 | `head -n N f`、`head -N f` | `[(1,N)]` | 否 |
| R9 | `head -n Y f \| tail -n K` | `[(Y-K+1, Y)]` | 否 |
| R10 | `tail -n +X f \| head -n K` | `[(X, X+K-1)]` | 否 |
| R11 | `tail -n K f` | 需知文件总行数，渲染时换算 | 否 |
| R12 | `cat f` | `[(1, None)]` | 否 |
| R13 | `nl -ba f`、`cat -n f` | `[(1, None)]` | 是 |
| R14 | 以上任意一种后接 `\| head -n K` | 与 head 取交集 | 继承 |

说明：
- 多段区间（R2、R4）的各段**独立渲染**，然后按原顺序拼接。
- `sed` 的脚本只接受由 `X,Yp`、`Xp` 用 `;` 连接的形式，出现任何其他 sed 命令（如 `/re/p`、`$p`、`=`）都不识别。
- `X > Y`、`X = 0` 时不识别。
- 路径的扩展名决定语言（6.3）。不支持的扩展名，**在识别阶段就放行**（不包标记，零开销）。

#### 5.3.2 搜索子命令（SearchSpec）

识别结果：`SearchSpec(tool, argv, head_k: int | None, single_file: str | None, has_line_numbers: bool)`。

| 形式 | 说明 |
|---|---|
| S1 | `grep ...`（含 `-r/-R/-n/-E/-F/-i/-w/-C/-A/-B/--include/...`） |
| S2 | `rg ...` |
| S3 | `git grep ...` |
| S4 | 以上后接 `\| head -n K` / `\| head -K` |

不识别的情况：
- 含 `-c/--count`、`-l/-L/--files-with(out)-matches`、`-q`、`-Z/--null`、`--json`、`-o`（v1）、`-v`（反向匹配，没有"命中位置"的含义）；
- 后面还有 `head` 以外的管道阶段（如 `| wc -l`、`| sort`、`| cut`）；
- 不确定输出里是否带行号：grep 需要 `-n`（或 `--line-number`）；rg 在非终端下默认不带行号，需要显式 `-n`；git grep 需要 `-n`。不带行号时只能做"按路径分层重排"，v1 直接不识别。

`single_file` 的判定：没有 `-r/-R/--recursive`，且 pattern 之后恰好只有一个路径操作数（pattern 是第一个非选项参数，除非用了 `-e/-f`）。这种情况下 grep 的输出不带路径（`12:text`），需要把路径补回来才能解析。参数解析不确定时，`single_file = None`；此时若输出里没有路径前缀，该块按原样回退。

### 5.4 标记协议与改写模板

标记前缀使用控制字符 `\x1e`（ASCII 记录分隔符，`printf` 中写作 `\036`），正常程序输出中几乎不会出现。每个块有一个从 0 开始的编号 `ID`。

#### 5.4.1 读取子命令的改写

原子命令 `S`，路径 `F`（已通过 5.3 的安全检查，用 `shlex.quote` 后记作 `QF`）：

```bash
{ printf '\036AST:B %d R\n' ID; \
  printf '\036AST:P %s\n' "$PWD"; \
  printf '\036AST:F %s ' "$(realpath -- QF 2>/dev/null)"; \
  [ -f QF ] && [ "$(wc -c < QF)" -le MAXB ] && base64 -w0 -- QF 2>/dev/null; \
  printf '\n\036AST:O\n'; \
  S; \
  __ast_rc=$?; \
  printf '\036AST:E %d %d\n' ID "$__ast_rc"; \
  (exit "$__ast_rc"); }
```

要点：
- **先快照、后执行原命令**。`S` 是只读的，所以快照内容就是 `S` 读到的内容。前面子命令写过的文件，快照时已经是写后的状态（满足 I7）。
- **文件内容用 base64 传输**：环境层以 `errors="replace"` 解码输出，直接传原始字节会在非 UTF-8 内容上丢失信息，也无法精确判断末尾是否有换行。base64 只增加传输量，不进入 LLM 上下文。
- `MAXB` 默认 2,000,000 字节。超过、文件不存在、`base64` 不可用时，快照为空，该块回退为原样输出（原样输出就在 `AST:O` 与 `AST:E` 之间）。
- `S` 的输出夹在 `AST:O\n` 与 `\036AST:E` 之间。`AST:E` 前面**不加换行**，这样原样输出末尾有没有换行都能被精确还原。
- 整个 `{ ...; }` 组的退出码等于 `S` 的退出码（`(exit rc)` 是组里最后一条命令），因此后面的 `&&` / `||` 行为不变（满足 I3）。
- 原命令的 stderr（如 `sed: can't read x: No such file`）照常出现在原样输出里，环境层本来就合并了 stdout 和 stderr。

#### 5.4.2 搜索子命令的改写

原子命令为 `G` 或 `G | head -n K`：

```bash
{ printf '\036AST:B %d S\n' ID; \
  printf '\036AST:P %s\n' "$PWD"; \
  printf '\036AST:O\n'; \
  G | head -n HMAX; \
  __ast_rc=$?; \
  printf '\036AST:E %d %d\n' ID "$__ast_rc"; \
  (exit "$__ast_rc"); }
```

- 原命令带 `| head -n K` 时，把 `K` 换成 `HMAX`（默认 500）。回退输出 = 扩大后输出的前 K 行，与原命令的输出逐字节相同（`head` 只取前缀，grep 输出是确定的）。两者的退出码都是 `head` 的退出码（未开 `pipefail` 时），保持一致。
- 原命令没有 `head` 时，不加 `head`（保持语义）。此时 `G` 原样执行，退出码是 grep 的退出码；输出过长由预算守卫处理。
- 如果 Agent 的命令里开了 `set -o pipefail`，或者片段之前出现过 `set -e`/`set -o pipefail`，则**不改写 head 的 K**（退出码可能不同），只做标注，不做"截断前重排"。

#### 5.4.3 输出拆解

在执行结果的 `output` 中，按 `\x1eAST:B` 定位各块。块外的文本（其他子命令的输出）原样保留。每个块解析出：`ID`、类型、`PWD`、realpath、文件快照（base64 解码）、原样输出、退出码。

出现以下任一情况，该块回退为原样输出：
- 块不完整，例如进程被超时杀掉、没有 `AST:E`；
- `ID` 对不上；
- base64 解码失败。

如果命令因超时（`returncode = -1`，有 `exception_info`）而失败，整条 action 回退：把输出中所有标记行去掉，尽量还原原样输出。

**注意**：命令超时时，原命令本身也会超时，所以 AST-Shell 不会额外引入超时。快照读文件的开销远小于 `sed` 本身。

---

## 6. 符号索引（Tree-sitter）与解析缓存

### 6.1 数据结构

```python
@dataclass
class Symbol:
    name: str            # 显示名，规则见 6.4
    kind: str            # function | method | class | struct | interface | trait | impl | enum | type | block
    start: int           # 1-based，含装饰器（Python decorated_definition）与 export 包装
    doc_start: int       # 紧贴 start 上方、中间无空行的连续注释行的首行；没有则等于 start
    sig_end: int         # 签名结束行（见 6.5）
    end: int             # 1-based，含
    parent: "Symbol | None"
    children: list["Symbol"]
    has_error: bool      # 子树内是否有 ERROR / MISSING 节点
    calls: list[str]     # 函数体内的调用名（见 9.4），延迟计算

@dataclass
class ParsedFile:
    sha1: str
    lang: str
    lines: list[str]               # 不含换行符；lines[i-1] 是第 i 行
    symbols: list[Symbol]          # 所有层级，按 start 排序
    innermost: list[Symbol | None] # 长度 n+1，innermost[i] 是包含第 i 行的最内层符号
    comment_only: set[int]         # 整行都落在注释节点内的行号
    skip_reason: str               # 非空表示该文件不做 AST 处理（如 "minified"）
```

### 6.2 各语言的符号节点

v1 支持：Python、Go、TypeScript/TSX、JavaScript/JSX、Rust。v2 计划：Java、C、C++、Ruby、PHP。

| 语言 | 容器类符号 | 函数类符号 | 其他 |
|---|---|---|---|
| Python | `class_definition` | `function_definition`；外层若是 `decorated_definition`，以它为准（start 取装饰器行） | — |
| Go | `type_declaration`/`type_spec`，值为 `struct_type`、`interface_type` | `function_declaration`、`method_declaration` | 顶层 `const_declaration` / `var_declaration`，行数 ≥ 10 时作为 `block` |
| TS/JS | `class_declaration`、`abstract_class_declaration`、`interface_declaration`、`enum_declaration` | `function_declaration`、`generator_function_declaration`、`method_definition`；`lexical_declaration`/`variable_declaration` 中值为 `arrow_function`/`function_expression` 的 `variable_declarator` | `type_alias_declaration`；外层若是 `export_statement`，start 取它的起始行 |
| Rust | `impl_item`、`trait_item`、`struct_item`、`enum_item`、`mod_item`（带 body 的） | `function_item`、`function_signature_item` | `macro_rules!` v1 不处理 |

层级规则：
- 只建立"容器 → 成员"两层关系（Rust 的 `mod` 可以继续嵌套容器）。
- **函数内部定义的函数/闭包不作为独立符号**，避免碎片化。
- 单行符号（`start == end`）照常收录，但不会被折叠。

### 6.3 扩展名映射

`.py` → python；`.go` → go；`.ts` → typescript；`.tsx` → tsx；`.js/.mjs/.cjs` → javascript；`.jsx` → javascript；`.rs` → rust。

不在表中的扩展名一律不处理（`.md`、`.json`、`.yaml`、`.toml`、`.sql`、`Makefile` 等）。

### 6.4 显示名规则

| 语言 | 例子 |
|---|---|
| Python | `ClassName.method`、`function` |
| Go | `(*ContainerService).CreateContainer`（保留接收者是否为指针）、`SetupAPI`、`type Settings` |
| TS/JS | `ClassName.method`、`functionName`、`const handler`（箭头函数） |
| Rust | `impl Foo`、`impl Display for Foo`、`Foo::method`、`fn helper` |

显示名超过 80 字符时，截断为前 77 字符加 `...`。

### 6.5 签名结束行 `sig_end`

**通用规则**：`sig_end` 是"body 字段之前最后一个子节点"的结束行。
- Python：body 是 `block`，它之前的最后一个子节点是 `:`。
- Go/TS/Rust：body 从 `{` 开始；规则同样适用，结果通常就是 `{` 所在的行。
- 没有 body 的节点（接口方法签名、类型别名）：`sig_end = end`。

### 6.6 跳过整个文件的情况

- 有任意一行超过 2000 字符（压缩代码 / 生成代码）。
- 行数超过 20000。
- 解析耗时超过 500 ms（Tree-sitter 本身很快，触发这条通常说明文件异常）。
- 根节点之下 `ERROR` 节点覆盖了超过 30% 的行。

### 6.7 解析缓存

- 键为 `(lang, sha1(内容字节))`，值为 `ParsedFile`。**路径不参与键**：同一份内容在不同路径下复用解析结果；同一路径内容一变，就自然成为新的键。
- 采用 LRU，默认 256 个条目，或总行数 200 万行，先到为准。
- **不存在"失效"操作**：渲染时总是用 5.4 拿到的最新快照算 sha1 再查缓存，不可能用到旧内容。缓存只省解析时间，不影响正确性。
- `calls` 字段延迟计算：只在 AutoSkeleton 需要时，才对对应符号的子树求值并写回。

---

## 7. 输出渲染格式规范

### 7.1 代码行

```
{n:>6}\t{原始行文本}
```

这与 `nl -ba` 和 GNU `cat -n` 的格式一致。

**两种格式**：
- **带行号格式**：每个代码行都是 `{n:>6}\t{原始行文本}`。
- **锚点格式**：代码行是原始行文本，不加任何前缀；真实行号靠占位行里的行号推出。每行行号前缀约 3 个 token，而一段几十行的代码只需要一条占位行，所以锚点格式省得多。

**什么时候用哪种**：
- 原命令带行号（R4、R5、R13）：始终用带行号格式。
- 块未被修改：保持原样（I2）。
- 原命令不带行号（R1、R2、R3、R6–R12），且块被修改过：同时生成两种格式，用锚点格式，除非它更长或 7.4 的锚点校验不通过，这时改用带行号格式。
- 锚点格式下，凡是没有占位行说明的跳行（例如 L2 骨架跳过的装饰器、注释、多行签名），都补一条 M-GAP 占位行，保证每一行都推得出行号。
- Agent 自己不知道起点的读取（含 `tail -n K` 这种从末尾计数的读取）被修改过：用带行号格式。

**锚点格式的推算规则**（Agent 读输出时按同样的规则就能算出每行行号）：
- 起点是 Agent 命令本身指定的第一行（`sed -n 'X,Yp'` 的 X，`cat`/`head` 的 1，`tail -n +X` 的 X）。
- 遇到以 `L{a}-{b}` 开头的折叠类占位行（M-FOLD-TAIL、M-BODY、M-BLOCK、M-SEEN、M-GAP、M-DUP）：下一个代码行是第 b+1 行。
- 遇到 M-SNAP-UP（`L{a}-{b} prepended: ...`）：下一个代码行是第 a 行。
- 其他占位行不改变行号；每个代码行之后行号加 1。
- 因此锚点格式下骨架**保留空行**（一个空行约 1 个 token，比锚点便宜）；带行号格式下骨架仍然丢弃空行。

### 7.2 占位行

```
{"":>6}\t[ast-shell] {消息}
```

- 行号列留空（6 个空格），这样不会被误认为第 n 行的代码。
- 固定前缀 `[ast-shell]`：便于 Agent 辨认，也便于事后统计。不用 `#` 开头，避免被 Agent 当作注释复制进代码。
- 消息中出现的行号一律写成 `L{a}-{b}`，并且是精确区间，Agent 可以直接用自己习惯的命令去读。
- **占位行要尽量短**：每一条占位行都会在后续每一轮被重复计费。因此不写读取命令、路径和行数，因为这些都能从 `L{a}-{b}` 和 Agent 自己刚发出的命令推出来；也不写类型词（method/function）。符号名在紧邻的上一行（签名行）已经出现时，也不再重复。

### 7.3 占位行的消息模板（全部集中定义在一处，便于改措辞和做消融）

| 代号 | 模板 | 示例 |
|---|---|---|
| M-FOLD-TAIL | `L{a}-{b}: end of {name} omitted` | `L180-211: end of (*MatchExpression).Provision omitted` |
| M-SNAP-DOWN | `extended to end of {name}` | `extended to end of (*MatchExpression).UnmarshalCaddyfile` |
| M-SNAP-UP | 带行号：`prepended: header of {name}`；锚点：`L{a}-{b} prepended: header of {name}` | `L18-23 prepended: header of Settings.load` |
| M-SNAP-IN | `inside {name} L{s}-{e}` | `inside SetupAPI L120-480` |
| M-CONT | `{name} continues to L{e}` | `SetupAPI continues to L480` |
| M-BODY | `L{a}-{b}: body folded{calls_part}` | `L46-65: body folded` |
| M-BLOCK | `L{a}-{b}: {desc} folded` | `L4-38: imports folded` |
| M-SEEN | `L{a}-{b}: {name} unchanged, shown {d} turns ago` | `L212-240: UnmarshalCaddyfile unchanged, shown 3 turns ago` |
| M-GAP | `L{a}-{b}: omitted`（仅锚点格式） | `L12-13: omitted` |
| M-DUP | `L{a}-{b}: shown above` / `L{a}-{b}: shown below` | `L44-46: shown below` |
| M-SKEL-HEAD | 带行号：`outline: bodies folded, line numbers real`；锚点：`outline: bodies folded` | |
| M-SKEL-MORE | `{k} more symbols omitted` | `23 more symbols omitted` |
| M-GREP-MORE | `+{k} more in {name}: L{l1},L{l2},...` | `+9 more in SetupAPI: L131,L140,L177,...` |
| M-GREP-OMIT | `{K}/{N} matches shown, defs first, tests last; omitted: {counts}` | `20/74 matches shown, defs first, tests last; omitted: 13 src, 41 test` |

字段说明：
- `{calls_part}` 默认为空。只有开启 `skeleton_calls` 时才会出现，形如 `; calls: a, b.c, d`，规则见 9.4。
- M-BODY 和 M-BLOCK 不写符号名，因为紧邻的上一行就是签名行。
- `{d}` 是距离上次展示过去的轮数（一次模型调用算一轮，同一轮里的多个 action 记为同一轮）。轮数比重复整条命令短得多，又足够让 Agent 往回找。轮数取自 Agent 的 `n_calls`（见 3.3）。
- M-GREP-MORE 最多列 8 个行号，超出部分写成 `...`。不重复符号的区间，因为同一符号的第一条匹配上已经标注过了。
- `{counts}` **只是按层统计的数字，不会渲染被省略的匹配本身**。最多 3 项（`src`、`comment`、`test`），数量为 0 的层不写，不写文件数，所以长度上限固定在几十个字符。定义层永远排在最前，如果连定义都被截掉，说明 K 太小，这时加一项 `def`。

长度估计：旧模板里的 M-BODY 示例约 160 字符（约 45 token），新模板不带调用摘要时约 40 字符（含行首缩进，约 10 token）。一个 40 个符号的骨架，大约能少 1400 token 的占位开销。

### 7.4 不变量校验（I1）

带行号格式：渲染块的每一行，必须满足以下两者之一：
- 匹配 `^ {6}\t\[ast-shell\] `；
- 匹配 `^ *(\d+)\t(.*)$`，且第 2 组等于 `lines[第 1 组 - 1]`。

锚点格式：从起点开始，按 7.1 的推算规则逐行推出行号 n，每个代码行必须等于 `lines[n - 1]`。

校验顺序：
1. 锚点格式在渲染时先校验，不通过就改为带行号格式（只多花 token，不丢信息）。
2. 最终输出再按所用格式校验一次，不通过就回退为原样输出。

两步都不通过的比例要记日志（`format` 字段），并有测试保证常见的扩展、折叠、骨架场景都走锚点格式，不会因为校验过严而全部退回带行号格式。这个校验要在生产代码里**始终开启**，代价可以忽略。

---

## 8. 算子 O1：SnapRead（区间读取的边界对齐）

### 8.1 适用范围

- 读取子命令（ReadSpec）中的每一段区间 `[X, Y]`；
- 文件可解析，且 `skip_reason` 为空；
- 不属于整文件式读取（整文件式读取交给 O2）；
- R11 需要先换算出 `X`；R12/R13 不属于本算子。

### 8.2 预处理

- `Y > n`（文件行数）时截到 `n`；`X > n` 时块回退（原样输出就是空的）。
- 计算 `hits = 会话状态中该文件当前 sha1 下的最近 grep 命中行集合`（见 13 节）。

### 8.3 判定规则

定义：
- `s_top`：包含第 X 行的最内层**函数类**符号，且 `s_top.start < X`；没有则为空。若最内层是容器类符号（如类），则看容器内是否有成员跨过 X；没有就不处理上切。
- `s_bot`：包含第 Y 行的最内层函数类符号，且 `s_bot.end > Y`；没有则为空。
- `starts_inside`：起始行落在 `(X, Y]` 内的符号数量（任何层级）。

**上切（只看 `s_top`）**：

| 条件 | 判定 | 动作 |
|---|---|---|
| `s_top` 含 hits 中的行，或 `starts_inside == 0`（窗口完全在这个函数内部） | 焦点 | 若 `X - s_top.doc_start ≤ SNAP_UP_MAX`（35），起点改为 `s_top.doc_start`；否则只改为 `s_top.start`，再超限则不改，并在块首插 M-SNAP-IN |
| 否则，且 `min(s_top.end, Y) - X + 1 ≥ TAIL_FOLD_MIN`（6） | 残尾 | 把 `[X, min(s_top.end, Y)]` 替换成一行 M-FOLD-TAIL |
| 否则 | 残尾太短 | 不动 |

**下切（只看 `s_bot`）**：

| 条件 | 判定 | 动作 |
|---|---|---|
| `Y - s_bot.start + 1 ≤ 3` 且 `s_bot` 不含 hits | 误碰下一个函数的头 | 不扩展，保留窗口内这几行，在其后插一行 M-CONT |
| `s_bot.end - Y ≤ SNAP_DOWN_MAX`（60） | 焦点 | 终点改为 `s_bot.end`，块尾插 M-SNAP-DOWN |
| 否则 | 长函数 | 不扩展，块尾插 M-CONT |

`s_top` 与 `s_bot` 是同一个符号（窗口完全落在一个函数内部，两头都被切）时，上下两条规则同时生效：先上扩，再按下切规则处理。

### 8.4 规则依据

- `SNAP_UP_MAX = 35`、`SNAP_DOWN_MAX = 60`：DeepSWE 窗口大小中位数为 70 行。允许的扩展量大约为一个中位窗口，扩展的代价通常小于再读一轮的代价（再读一轮意味着整个上下文要重新发送一次）。这两个值都是配置项，最终取值由第 19.2 节的离线校准确定。
- 残尾折叠阈值 6 行：短于 6 行时，折叠占位行本身的长度和原文差不多，不值得折叠。

### 8.5 输出构造

1. 根据最终区间 `[X', Y']`，逐行输出 `%6d\t` 代码行。
2. 被折叠的残尾区间替换为一行占位。
3. 依次附加 M-SNAP-UP、M-SNAP-DOWN、M-CONT 占位行。M-SNAP-UP 放在块首，其余放在块尾。
4. 如果启用了 O4，在此步之前先执行 SeenFold 的替换（第 11 节）。
5. 没有任何规则触发：回退原样输出（I2）。

### 8.6 多段区间

R2/R4 的多段区间各自独立对齐。如果对齐后相邻两段重叠或首尾相接，合并成一段：同一行只输出一次，开头占位行只取起点最早那段的，结尾占位行只取终点最晚那段的，中间各段的首尾占位行全部丢弃。

同一条命令里多次读取同一文件（例如 `sed -n 'a,bp' f && sed -n 'c,dp' f`，且两次读到的内容相同）时，某次读取**因扩展而新增**的行，如果已经由前面的读取输出过，或后面的读取本来就要输出，就折叠为 M-DUP 占位行 `L{a}-{b}: shown above` / `L{a}-{b}: shown below`。Agent 自己请求的行一律保留，不做跨读取去重。

---

## 9. 算子 O2：AutoSkeleton（整文件读取的骨架化）

### 9.1 触发条件（统一定义，取代旧版本中互相矛盾的三个阈值）

**整文件式读取**，满足以下任一即可：
- R12、R13（`cat f`、`nl -ba f`、`cat -n f`）；
- R8（`head -n N f`），且 `N ≥ 0.8 × n`；
- 只有一段区间，且 `X ≤ max(1, 第一个顶层符号的 doc_start)`，并且 `min(Y, n) ≥ 0.8 × n`。

**规则 A（单文件触发）**：整文件式读取，且该块原样输出的字符数 `> SKELETON_MIN_CHARS`（默认 6000，约 1500 token）→ 骨架化。否则原样完整输出。

**规则 B（单 action 预算兜底）**：见第 12 节。预算超限时，**区间读取块也可以被降级**，降级方式是"区间内骨架"，即只对区间内的符号应用本节的折叠规则。

**为什么用字符数，不用行数**：同样 240 行，Python 文件约 8000 字符，带长标签的 Go 结构体可达 3 万字符（轨迹中的 `settings.go`）。行数无法反映成本。

**为什么小文件不骨架化**：骨架化的风险是 Agent 随后需要展开某个函数，多出一轮，而多一轮意味着整个历史上下文被重新发送一次。对只有一两千 token 的文件，省下的远不足以抵消这个风险。6000 是初始值，由第 19.2 节的离线校准确定。

### 9.2 骨架构造

逐行扫描文件，按以下规则决定每一行输出原文还是被折叠：

1. **文件头部**：第一个顶层符号的 `doc_start` 之前的所有行（package、import、模块级常量、文件注释）原样输出。若这一段超过 `HEADER_MAX_LINES`（60），只保留前 40 行和后 10 行，中间折叠为 M-BLOCK（`desc = "imports/header"`）。
2. **顶层非符号代码**（模块级语句、大字面量）：连续 ≤ 30 行原样输出；超过时保留前 8 行和后 3 行，中间折叠为 M-BLOCK（`desc = "top-level statements"`）。
3. **容器类符号**（class、impl、interface、struct 等）：
   - 头部（`doc_start` 到 `sig_end`）原样输出；
   - 成员之间的非成员行（字段、类变量）：连续 ≤ 30 行原样输出；更长时保留前 15 行、与 hits 距离 ±2 以内的行、后 10 行，其余折叠为 M-BLOCK（`desc = "fields"`）。这一条专门处理 `settings.go` 那种超长结构体；
   - 成员递归应用第 4 条；
   - 结束行（Go/TS/Rust 的 `}`）原样输出。
4. **函数类符号**，满足任一条件则**完整展开**：
   - 包含 hits 中的行；
   - 函数体（`sig_end + 1` 到 `end`，花括号语言不含最后一行 `}`）≤ `BODY_KEEP_MAX`（12）行；
   - 显示名中的最后一段标识符出现在任务文本中（任务文本来自 `ASTShell.reset`，见 3.3；匹配时忽略长度 < 4 的名字）；
   - `has_error` 为真（让 Agent 看到语法错误现场）。

   否则：
   - 输出 `doc_start` 到 `sig_end`（装饰器、注释、完整多行签名）；
   - Python：若函数体第一个语句是文档字符串，再输出它的第一行；
   - 其余函数体折叠为一行 M-BODY；
   - 花括号语言：再原样输出最后的 `}` 行。
5. 块首插入一行 M-SKEL-HEAD。

### 9.3 骨架自身过长时的降级阶梯

| 级别 | 内容 |
|---|---|
| L0 | 原样输出 |
| L1 | 9.2 的骨架 |
| L2 | 大纲：每个符号只保留签名首行加一行 M-BODY（不含 calls），容器的字段全部折叠 |
| L3 | 大纲截断：只保留前若干个符号，末尾一行 M-SKEL-MORE |

规则 A 只产出 L1。L1 仍超过单块上限 `BLOCK_MAX_CHARS`（默认 7000）时，降到 L2，再超就降到 L3。规则 B 也按这个阶梯逐级降级。

### 9.4 折叠行中的调用摘要（只出现在 M-BODY 中）

- **只用于 AutoSkeleton 的 M-BODY 占位行**，包括规则 B 降级产生的区间内骨架。SnapRead 的残尾折叠不附带调用摘要。L2/L3 也不附带。
- 提取方式：遍历被折叠的函数体子树中的调用节点（Python：`call` 的 `function` 字段；Go/TS/JS/Rust：`call_expression` 的 `function` 字段；Rust 的方法调用即 `call_expression` 中的 `field_expression`）。取调用目标文本，规范化为最后两段（`a.b.c.d` → `c.d`），去掉参数和泛型。
- 过滤掉通用调用，名单按语言配置。例如 Python 的 `len`、`str`、`int`、`isinstance`、`print`、`super`、`getattr`、`logger.*`；Go 的 `fmt.Errorf`、`fmt.Sprintf`、`errors.New`、`append`、`len`、`make`、`slog.*`；TS/JS 的 `console.*`、`JSON.stringify`；Rust 的 `clone`、`unwrap`、`to_string`、`into`。
- 按首次出现顺序去重，最多 `CALLS_MAX`（6）个；整个 `calls_part` 不超过 100 字符。
- **v1 默认关闭**（`skeleton_calls: false`）。这是一个待验证的假设：它可能帮 Agent 省去展开，也可能诱导 Agent 顺着调用链继续读、反而增加轮数，而且它是占位行里最长的部分。主实验不开启，之后作为可选的附加实验。

### 9.5 与 SnapRead 的衔接

Agent 按签名行号和 M-BODY 给出的区间去读（例如 `sed -n '45,66p' f`），这是一次区间读取，会走 O1。由于区间恰好等于符号边界，O1 不会触发任何改写，Agent 读到的就是完整函数原文，不会出现"展开后又被折叠"的循环。如果 Agent 只读了 M-BODY 给出的函数体区间（不含签名行），O1 的上切规则会把签名补上，结果相同。

---

## 10. 算子 O3：SmartGrep（搜索结果的符号标注与重排）

### 10.1 输出行解析

带行号的 grep/rg/git grep 输出有以下几种行：

| 行 | 正则 | 说明 |
|---|---|---|
| 匹配行 | `^(?P<path>.+?):(?P<line>\d+):(?P<text>.*)$` | |
| 上下文行 | `^(?P<path>.+?)-(?P<line>\d+)-(?P<text>.*)$` | `-A/-B/-C` 产生 |
| 分组分隔 | `^--$` | |
| 单文件匹配行 | `^(?P<line>\d+):(?P<text>.*)$` | 仅当 `single_file` 已知 |
| 单文件上下文行 | `^(?P<line>\d+)-(?P<text>.*)$` | 仅当 `single_file` 已知 |

路径中可能含 `-` 或 `:`，第一种正则存在歧义。处理方法：用 `PWD` 拼出绝对路径，若在 10.4 取到的文件集合里存在、且该行文本与文件第 `line` 行一致，才采信这次解析；否则这一行原样保留、不标注。

**带上下文（`-A/-B/-C`）的输出，v1 只做标注、不做重排和截断**，以保持上下文分组的完整性。

### 10.2 命中分层

| 层 | 条件 | 标签 |
|---|---|---|
| T1 定义 | 行号落在某符号的 `[start, sig_end]` 内 | `def` |
| T2 源码 | 路径不匹配测试规则，且该行不在 `comment_only` 中 | 无标签 |
| T3 注释 | 路径不匹配测试规则，且该行在 `comment_only` 中 | `comment` |
| T4 测试 | 路径匹配测试规则 | `test` |
| T0 未知 | 文件不支持或不可解析 | 不分层，排在 T2 位置，不标注 |

测试路径规则（可配置）：`(^|/)(tests?|__tests__|spec|testdata|fixtures?)(/|$)`、`_test\.(go|py)$`、`(^|/)test_[^/]*\.py$`、`\.(test|spec)\.[jt]sx?$`、`(^|/)examples?/`。

**字符串字面量不降级**：Agent 经常用报错信息去搜代码，命中字符串的行往往正是目标。

### 10.3 标注格式

匹配行改写为：

```
{path}:{line}: [{tag }in {name} L{s}-{e}] {text}
```

其中 `{name}` 是包含该行的最内层符号。该行不在任何符号内时（import、模块级语句等），整个 `[...]` 标注都省略，这一行保持原样输出。分层仍照常进行，只影响重排顺序，不显示标签。
- T1 的写法：`[def L{s}-{e}]`。签名跨多行时也不补全，只显示 grep 命中的这一行。
- 上下文行不加标注。

### 10.4 获取命中文件的内容

grep 输出里出现的文件，事先并不知道是哪些，所以需要第二次 `execute` 去取：

```bash
cd PWD && for f in F1 F2 ...; do printf '\036AST:F %s ' "$f"; [ -f "$f" ] && [ "$(wc -c < "$f")" -le MAXB ] && base64 -w0 -- "$f"; printf '\n'; done
```

- 只取支持语言的文件，最多 `GREP_MAX_FILES`（40）个，按命中次数从多到少挑选。超出的文件不标注（T0）。
- 这次调用不计入 Agent 的步数，也不经过 AST-Shell 的改写；耗时计入日志。
- **一致性**：文件是在整条命令执行完之后才读取的。如果同一条命令里 grep 之后还有写操作，内容可能已经变了。因此每条命中都要校验"该行文本 == 文件第 `line` 行"，不一致就不标注这一条。如果某个文件超过一半的命中都不一致，整个文件都不标注。
- v1 每次都重新读取，不复用之前的快照。判断"之后没有写入"恰恰是本方案想避免的猜测，而且 base64 传输的开销只有毫秒级。

### 10.5 重排与截断

仅当以下条件全部满足时才重排：
- 没有上下文选项；
- 匹配行总数 `N` 大于原 head 的 K，或者没有 head 但 `N > GREP_RERANK_MIN`（40）。

做法：
1. 按 T1 → T2 → T3 → T4 稳定排序（同一层内保持 grep 的原顺序）。
2. **同符号内去重**：同一符号内超过 3 条匹配时，保留前 2 条，其余合并为一行 M-GREP-MORE，最多列出 12 个行号。
3. 有 K 时，取前 K 条；在末尾附一行 M-GREP-OMIT，只按层给出被省略的条数，例如 `omitted: 5 comment, 37 test`。
4. 没有 K 时，交给第 12 节的预算守卫。

**紧凑模式**（仅在重排发生时启用）：按文件分组，路径只写一次：

```
{path}
  {line}: [{tag }in {name} L{s}-{e}] {text}
  ...
```

### 10.6 写入会话状态

所有成功解析的匹配行（无论最终是否展示），以 `(realpath, sha1) → 行号集合` 的形式写入 `recent_hits`（见 13 节），供后续的 O1 和 O2 判断焦点。

---

## 11. 算子 O4：SeenFold（跨轮次已读未改符号折叠）

### 11.1 记录什么

每次一个渲染块（或回退后的原样读取块）被**完整地放进最终 observation**之后，对其中完整展示过的每个函数类符号，记录：

```
seen[(realpath, name, sha1(符号源码文本))] = (turn_index, action_index)
```

- 只记录真正展示了 `start..end` 全部行的符号。被折叠、被截断的不算。
- 如果整条 action 的最终输出超过观察截断阈值（10000 字符），这条 action **不记录任何 seen**：截断会去掉中间部分，无法确定哪些行真正被 Agent 看到了。
- 键里用的是**符号文本**的哈希，不是文件哈希：文件别处改了、行号漂移了，没改过的函数仍能识别出来；被改过的函数，哈希一定变化。

### 11.2 什么时候折叠

渲染 O1 或 O2 块时，对块内每个**完整出现**的函数类符号 `s`，同时满足以下条件才把它替换为一行 M-SEEN：
1. `seen` 中有它的记录，且距离当前不超过 `SEEN_WINDOW`（默认 30 个 action）；
2. `s` 不是本次读取的焦点：不是 8.3 中判定为焦点的 `s_top`/`s_bot`，也不是区间内唯一的符号；
3. 本次读取的区间不是与某次历史读取完全相同的区间（完全重复说明 Agent 明确想再看一遍）；
4. `s` 的函数体超过 `BODY_KEEP_MAX` 行（短函数直接展示，比占位行划算）。

### 11.3 为什么默认关闭

mini-swe-agent 不截断历史，之前展示过的内容确实还在上下文里，所以折叠在信息上是安全的。但 Agent 重读往往是因为在长上下文里"找不到"之前的内容，折叠后可能迫使它更努力地回看，效果不确定。因此 O4 默认关闭（`seen_fold: false`），作为消融实验的一项。

---

## 12. 输出预算守卫

### 12.1 预算对象

观察截断是对每个 action 单独生效的：`format_toolcall_observation_messages` 对每个 output 单独渲染模板。因此预算也按 action 计算：

```
BUDGET = budget_ratio × 观察截断阈值 = 0.92 × 10000 = 9200 字符
```

如果评测配置的截断阈值不同（第 0 步需确认），只需修改配置项 `observation_char_limit`。

### 12.2 算法

1. 计算 `total = 块外原样文本长度 + Σ 各块渲染后的长度`。
2. 若 `total ≤ BUDGET`，结束。
3. 否则，找出当前最长、且还能降级的块，降一级：

   | 块类型 | 降级顺序 |
   |---|---|
   | 读取块（O1 结果） | 区间内骨架 L1 → L2 → L3 |
   | 读取块（O2 结果） | L1 → L2 → L3 |
   | 搜索块（无 K 且可重排） | 重排 + 同符号去重 → 截断到 `BUDGET` 能容纳的条数，附 M-GREP-OMIT |
   | 回退为原样输出的块、块外文本 | 不可降级 |

4. 重复第 3 步，直到 `total ≤ BUDGET` 或没有可降级的块。
5. 如果仍超限（例如块外的测试输出本身就很长），就保持当前结果，交给模板截断兜底。

### 12.3 注意

- 原命令输出本来就不超过 BUDGET 时，预算守卫不做任何事。
- 区间读取块只有在预算超限时才会被骨架化。正常情况下，Agent 显式要求的区间不会被折叠函数体。

---

## 13. 会话状态

```python
@dataclass
class SessionState:
    action_index: int = 0
    turn_index: int = 0                            # 等于 Agent 的 n_calls；用于 M-SEEN 的 {d}
    recent_hits: dict[tuple[str, str], set[int]]   # (realpath, sha1) -> 行号；sha1 不同即视为另一份内容
    hits_age: dict[tuple[str, str], int]           # 最后一次写入时的 action_index
    seen: dict[tuple[str, str, str], tuple[int, int]]
    read_ranges: dict[tuple[str, str], list[tuple[int, int]]]  # 用于 11.2 第 3 条
    stats: Counters                                # 第 16 节
```

- 生命周期：一个任务实例一份（随 Agent 或 Environment 对象创建）。
- `recent_hits` 只保留最近 `HITS_WINDOW`（默认 10 个 action）内写入的项。查询时用快照的 sha1 去匹配，文件改过之后，旧的命中自然失效。
- 并行 tool call：同一轮中的多个 action 按顺序执行，共享状态，`action_index` 逐个递增。

---

## 14. 放行规则与边界情况

### 14.1 整条 action 放行（不解析、零开销）

- 含 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`（I6）。
- 不包含任何候选关键词：`sed`、`nl`、`cat`、`head`、`tail`、`awk`、`grep`、`rg`。先做子串检查，绝大多数测试和编辑命令在这一步就放行了。
- 长度超过 20000 字符（通常是写文件的 here-doc）。
- 拆分器返回 `None`。
- 配置 `enabled: false`。

### 14.2 块级回退（只影响该块）

- 文件不支持、不存在、过大、被判定跳过（6.6）。
- 快照为空或解码失败。
- 原命令退出码不为 0（读取块）；搜索块的退出码不在 `{0, 1}` 中。grep 无匹配时退出码为 1，此时原样输出为空，不需要渲染。
- 原样输出与快照不一致：比如 `sed -n '10,20p'` 的原样输出应等于快照的第 10–20 行。**这一条要校验**，用来防御识别错误（例如 sed 的方言差异）。
- 渲染异常、不变量校验失败（I1）。

### 14.3 其他

- CRLF 文件：`lines` 按 `\n` 切分并保留行尾的 `\r`。比较时原样比较；输出时也保留 `\r`，与原命令输出一致。
- 没有末尾换行的文件：最后一行照常处理；渲染块统一以 `\n` 结尾。
- 制表符：原样保留。
- 超长行（≤ 2000 字符的部分）：原样保留，不截断单行。
- `git show`、`git diff`、`cat patch.txt`、非源码文件：天然不会被识别。

---

## 15. 配置项全表

配置放在 `agent.ast_shell` 下（见 3.3）。

| 键 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关 |
| `snap_read` | `true` | O1 |
| `skeleton` | `true` | O2 |
| `skeleton_calls` | `false` | 9.4 调用摘要，v1 默认关闭 |
| `smart_grep` | `true` | O3 |
| `grep_rerank` | `true` | 10.5 重排与截断（关闭时只标注） |
| `seen_fold` | `false` | O4 |
| `budget_guard` | `true` | 第 12 节 |
| `observation_char_limit` | `10000` | 与 observation 模板一致 |
| `budget_ratio` | `0.92` | |
| `snap_up_max` | `35` | |
| `snap_down_max` | `60` | |
| `tail_fold_min` | `6` | |
| `skeleton_min_chars` | `6000` | 规则 A |
| `block_max_chars` | `7000` | 9.3 |
| `body_keep_max` | `12` | |
| `header_max_lines` | `60` | |
| `calls_max` | `6` | |
| `grep_head_max` | `500` | HMAX |
| `grep_rerank_min` | `40` | |
| `grep_max_files` | `40` | |
| `hits_window` | `10` | 以 action 计 |
| `seen_window` | `30` | 以 action 计 |
| `max_file_bytes` | `2000000` | |
| `languages` | `[python, go, typescript, tsx, javascript, rust]` | |
| `test_path_patterns` | 见 10.2 | |
| `log_original` | `false` | 为 `true` 时把原样输出全文写入日志，仅用于分析，会显著增大轨迹文件 |

每个算子都能单独开关，消融实验只需通过命令行 `-c agent.ast_shell.snap_read=false` 之类的参数切换。

---

## 16. 日志与可观测性

每个 output 的 `extra` 中写入 `ast_shell` 字段。`format_toolcall_observation_messages` 会把 `output["extra"]` 合并进消息的 `extra`，随轨迹一起保存：

```json
{
  "ast_shell": {
    "v": 1,
    "status": "rewritten | passthrough | fallback",
    "passthrough_reason": "no_keyword | submit | split_failed | ...",
    "orig_chars": 18931,
    "final_chars": 7420,
    "latency_ms": {"parse": 3.1, "render": 1.2, "grep_fetch": 41.0},
    "blocks": [
      {"id": 0, "type": "read", "path": "/app/x.go", "lang": "go",
       "ops": ["snap_down", "fold_tail"], "orig_chars": 5120, "final_chars": 3010,
       "range_req": [180, 260], "range_out": [212, 285]},
      {"id": 1, "type": "search", "ops": ["annotate", "rerank", "omit"],
       "matches": 212, "shown": 50, "tiers": {"def": 3, "src": 20, "comment": 2, "test": 187}},
      {"id": 2, "type": "read", "status": "fallback", "reason": "unsupported_lang"}
    ]
  }
}
```

- `orig_chars` 是把每个块的原样输出拼回去后的长度。由于原命令确实执行了，这个数字是精确的反事实值，可以直接用来计算"本步节省"。
- 环境或 Agent 的 `serialize()` 中附加整个任务的汇总计数：各算子触发次数、回退原因分布、累计节省字符数。
- 消息里的 `raw_output` 保存的是最终输出。原样输出全文只在 `log_original: true` 时保存。

---

## 17. 代码结构与接口

```
src/minisweagent/astshell/
├── __init__.py        # ASTShell、ASTShellConfig 对外导出
├── config.py          # ASTShellConfig（pydantic BaseModel，与仓库其他配置风格一致）
├── command.py         # split_top_level、recognize_read、recognize_search、rewrite、parse_markers
├── symbols.py         # 语言表、parse_file、Symbol、ParsedFile、ParseCache、calls 提取
├── render.py          # snap_read、skeleton（L1/L2/L3）、smart_grep、seen_fold、消息模板
├── budget.py          # 预算守卫
└── shell.py           # ASTShell.run：编排、回退、会话状态、日志
src/minisweagent/agents/astshell.py        # ASTShellAgent(InteractiveAgent) 与 env 代理，见 3.3
harbor_ext/astshell_mini.py                # ASTShellMiniSweAgent(harbor MiniSweAgent)，只重写 install()，见 3.3.1
src/minisweagent/config/benchmarks/swebench_astshell.yaml  # 或 DeepSWE 对应配置的覆盖片段
tests/astshell/
├── fixtures/          # py/go/ts/rs 示例文件
├── test_command.py
├── test_symbols.py
├── test_render.py
├── test_shell.py
└── test_corpus.py     # 用 DeepSWE 真实命令做的语料测试（见 18.3）
```

预计规模：核心约 1000–1300 行，测试约 600–800 行。遵循仓库的 `AGENTS.md`：Python 3.10+、类型标注、`pathlib`、尽量少的注释、不随意捕获异常。

这里 I5 要求"失败即回退"，所以 `shell.py` 中按块捕获异常并记录原因，这是**唯一允许的宽捕获点**；其他模块不捕获异常，让问题在测试中暴露。

### 17.1 主要接口

```python
class ASTShell:
    def __init__(self, config: ASTShellConfig): ...
    def reset(self, *, task: str = "") -> None:
        """每个任务开始时调用：清空会话状态，记录任务文本。"""
    def run(self, action: dict, execute: Callable[[dict], dict], *, turn: int = 0) -> dict:
        """返回与 execute 相同结构的 dict，并在 extra 中带 ast_shell 日志。"""

# command.py
def split_top_level(cmd: str) -> list[Segment] | None
def recognize(segment: Segment) -> ReadSpec | SearchSpec | None
def rewrite(cmd: str, segments: list[Segment], specs: dict[int, Spec]) -> str
def parse_markers(output: str) -> tuple[list[str | RawBlock], list[str]]   # 交错的块外文本与块；错误列表

# symbols.py
def parse_file(content: bytes, lang: str) -> ParsedFile
class ParseCache:
    def get(self, content: bytes, lang: str) -> ParsedFile

# render.py
def render_read(pf: ParsedFile, spec: ReadSpec, path: str, state: SessionState, cfg) -> Rendered | None
def render_search(raw: str, spec: SearchSpec, files: dict[str, ParsedFile], pwd: str, state, cfg) -> Rendered | None
```

`Rendered` 包含渲染文本、使用的算子列表、可降级的级别序列（供预算守卫调用）。

---

## 18. 测试计划

### 18.1 单元测试

- **拆分器**：引号、转义、`$( )`、反引号、子 shell、花括号组、here-doc（含 `<<-` 和带引号的定界符）、`&&`/`||`/`;`/换行混合；拼回后必须等于原串。
- **识别器**：5.3 表中每种形式的正例，以及每一条"不识别"规则的反例。
- **符号**：每种语言一个 fixture，断言符号名、kind、`start`/`doc_start`/`sig_end`/`end`。覆盖 Python 装饰器、Go 方法接收者、TS 的 `export` 和箭头函数、Rust 的 `impl` 与 trait impl、多行签名、单行函数、含语法错误的文件。
- **O1**：8.3 表中的每一行规则各一个用例，加上"无规则触发 → 逐字节等于原样输出"。
- **O2**：9.2 的每条规则；L1/L2/L3 的降级；调用摘要的过滤与上限。
- **O3**：每种输出行格式、分层规则、同符号去重、紧凑模式、行文本不一致时不标注。
- **O4**：11.2 的每个条件；文件改动后、行号漂移的情况。
- **预算守卫**：多块组合下的降级顺序。

测试风格遵循 `AGENTS.md`：用 pytest；`parametrize` 的第一个参数用元组、第二个用列表；**不 mock**，直接用真实的 `LocalEnvironment.execute` 在临时目录上运行。

### 18.2 不变量的性质测试

对 fixture 仓库随机生成区间读取命令（随机文件、随机 `X`、`Y`，随机选用 R1–R14 的形式），以及随机的 grep 命令，断言：
- I1：每个非占位行与文件对应行一致；
- I3：改写前后退出码相同；
- 回退输出与原命令输出逐字节相同；
- 拼接后，块外文本与原命令对应部分逐字节相同。

### 18.3 真实命令语料测试

从 DeepSWE 轨迹中抽取约 2 万条真实命令，做以下检查（不需要执行）：
- 拆分器不抛异常，且拼回结果等于原串；
- 统计识别率：读取类、搜索类分别有多少被识别、未识别的原因分布。用于发现常见但没覆盖的写法；
- 改写后的命令能通过 `bash -n` 语法检查。

### 18.4 执行等价测试

在一个真实小仓库上（例如把 `mini-swe-agent` 自身作为 fixture 仓库），挑 200 条语料中识别成功的命令，把路径替换为仓库中存在的文件，分别执行原命令和改写命令，比较退出码与回退输出。

---

## 19. 实验方案

### 19.1 离线回放（零 API 成本，先做）

轨迹里没有仓库快照，所以离线回放需要任务镜像：
- 对每条轨迹，在该任务的原始镜像中，**只回放第一次编辑之前的探索步骤**（第一个含写操作的 action 之前）。这段前缀不依赖 Agent 修改后的状态，可以精确复现；
- 对每个 action 分别得到原样输出和 AST-Shell 输出。

产出指标：
- 单步 observation 字符缩减率，按算子、按语言分别统计；
- 原本超过 10000 字符被截断的 action 中，有多少比例在 AST-Shell 下不再超限；
- **"翻页"命中率**：原轨迹中第 t 步读了 `[X, Y]`、第 t+1 步又读同一文件的相邻区间（起点在 `Y ± 20` 内），其中有多少比例在第 t 步已被 SnapRead 覆盖；
- **"读后展开"率**：对原轨迹中被 AutoSkeleton 折叠掉的函数，统计后续步骤中被 grep 命中、被 `sed` 读取或被编辑的比例。这个比例就是 9.1 中的展开概率 P；
- 渲染延迟分布。

### 19.2 阈值校准

用 19.1 的数据，对 `skeleton_min_chars`、`snap_down_max`、`snap_up_max` 做网格搜索，选使下面期望收益最大的取值：

\[
\text{收益} = \text{减少的字符} - P(\text{需要多一轮}) \times C_{\text{round}}
\]

其中 \(C_{\text{round}}\) 用该步之后的上下文长度乘以缓存命中单价、再加上平均输出成本来近似。

注意：离线回放无法观察 Agent 行为的变化（它看到不同输出后可能走不同的路径），所以校准只能给出初值，最终结论以 19.3 的在线实验为准。

### 19.3 在线实验

- 先做小规模健全性检查：10–20 个 DeepSWE 实例，Baseline 与 Full 对比，重点检查有没有因输出格式变化导致的异常行为（例如 Agent 把 `[ast-shell]` 占位行复制进代码）。
- 主实验：
  - 至少 2 个模型（建议一个强模型、一个便宜模型；SWE LSP 的结果显示两类模型的节省幅度差异很大）；
  - 至少 100–300 个实例，每个配置跑 3 次以估计方差；
  - 配置：Baseline、Full。
- 消融：

  | 配置 | 关闭项 |
  |---|---|
  | w/o O1 | `snap_read=false` |
  | w/o O2 | `skeleton=false` |
  | + calls | `skeleton_calls=true`（可选，主实验之后再做） |
  | w/o O3 | `smart_grep=false` |
  | O3 只标注 | `grep_rerank=false` |
  | w/o budget | `budget_guard=false` |
  | + O4 | `seen_fold=true` |

- 可选对比：SWE LSP（若其代码可获取）、仅增加 Tree-sitter 版本的 `read_def` 类工具（即"只加工具、不改输出"），用以复现并对照 SWE LSP 关于"工具 vs 工作流嵌入"的结论。

### 19.4 指标

| 类别 | 指标 |
|---|---|
| 效果 | 解决率（Pass@1） |
| 成本 | 总 token、缓存未命中 token、缓存命中 token、输出 token、按统一价目表计算的 API 成本、每个已解决实例的成本 |
| 过程 | 平均轮数、探索阶段轮数（第一次编辑之前）、各类命令次数、超过 10000 字符的 action 比例、翻页次数 |
| 行为 | 占位行中给出的 `sed` 命令被 Agent 直接采用的比例；骨架化后被展开的比例；Agent 把占位行内容写进文件的次数（应为 0） |
| 开销 | 每个 action 的额外耗时（P50/P99）、SmartGrep 的二次读取耗时 |

**关于成本的提醒**：DeepSWE 中提示缓存的命中率很高。抽查的一条轨迹中，总输入 4405 万 token，其中缓存命中 4271 万（约 97%）。在这种情况下，减少 observation 字符带来的成本下降，会远小于 token 数的下降；而减少轮数的效果更明显，因为每少一轮就少一次整个上下文的读取和一次输出生成。所以论文中必须把 token 和成本分开报告，并按"缓存未命中 / 缓存命中 / 输出"三类分别计价（SWE LSP 也是这样做的）。

---

## 20. 风险与开放问题

| 风险 | 影响 | 缓解 |
|---|---|---|
| Agent 不理解 `[ast-shell]` 占位行，或把它当作代码复制进文件 | 编辑失败、补丁被污染 | 固定前缀、行号列留空；在线实验统计"写入文件"次数；必要时在系统提示中加一句说明（作为额外消融项） |
| 折叠导致 Agent 缺少关键信息、多出展开轮次 | 轮数增加 | 阈值校准；焦点规则（hits、任务文本）；骨架只在大文件上触发 |
| 行号错位，导致 Agent 按行号编辑时改错位置 | 严重 | I1 校验始终开启；原样输出与快照一致性校验（14.2） |
| 识别器误判命令语义（例如 sed 方言差异） | 输出错误 | 只识别严格形式；原样输出与快照的一致性校验 |
| SmartGrep 重排打乱了 Agent 依赖的顺序 | 理解偏差 | 层内保持原顺序；只在会被截断或输出过长时才重排；做消融 |
| Harbor 的集成方式与预期不同 | 无法接入 | 第 0 步先确认；核心与集成方式无关 |
| 容器内模式下 Tree-sitter 安装失败 | 无法运行 | 初始化阶段直接报错，不静默降级 |
| 提示缓存 | — | 只改当前 action 的输出，不改历史（I8），不破坏缓存前缀 |
| 收益被缓存稀释 | 成本节省幅度小于 token 节省幅度 | 论文中如实分开报告；把轮数减少作为主要卖点之一 |

**开放问题**：
1. 规则 A 的阈值是否应该随"当前上下文长度"自适应？上下文越长，多一轮的代价越大，骨架化应该越保守。
2. 任务文本作为焦点信号的效果如何？
3. 是否需要在第一次触发时，向 Agent 简短说明占位行的含义？这一项作为消融。
4. v2 是否加入"带 NR 打印的 awk"、`grep -o`、上下文模式下的重排？
5. traceback 中的文件位置也可以用同样的方式标注（SWE LSP 做了类似的事）。v1 不做，原因是测试输出在累计成本中只占约 4%。

---

## 附录 A：端到端示例

以下示例中的文件内容是为了说明而构造的。行号和函数名仿照轨迹中真实出现的 Go 代码风格。

### A.1 SnapRead：下切扩展加残尾折叠

文件 `modules/caddyhttp/celmatcher.go` 共 600 行，其中：
- `(*MatchExpression).Provision`：L150–L211
- `(*MatchExpression).UnmarshalCaddyfile`：L212–L240
- `(*MatchExpression).Match`：L242–L300

最近一次 grep 命中了第 212 行。

Agent 的命令：

```bash
cd /app && sed -n '180,230p' modules/caddyhttp/celmatcher.go
```

判定：
- `s_top = Provision`，不含 hits，且 `(180, 230]` 内有其他符号起始（第 212 行）→ 残尾，共 32 行 ≥ 6 → 折叠；
- `s_bot = UnmarshalCaddyfile`，`end - Y = 10 ≤ 60` → 扩展到 L240。

输出：

```
      	[ast-shell] L180-211: end of (*MatchExpression).Provision omitted
   212	func (m *MatchExpression) UnmarshalCaddyfile(d *caddyfile.Dispenser) error {
   213		d.Next() // consume matcher name
   ...（逐行到 240）
   240	}
      	[ast-shell] extended to end of (*MatchExpression).UnmarshalCaddyfile
```

原输出 51 行，新输出 31 行，且目标函数完整；原轨迹中 Agent 在这之后还会读一次 `sed -n '230,260p'`，现在这一步就省掉了。

### A.2 SmartGrep：标注、重排、截断

Agent 的命令：

```bash
cd /app && grep -rn "ContainerInspectWithCompatibility" backend/ | head -20
```

扩大到 500 行后，共有 74 条匹配，其中 41 条在 `_test.go` 中。重排后输出前 20 条（紧凑模式）：

```
backend/pkg/libarcane/container.go
  31: [def L31-58] func ContainerInspectWithCompatibility(ctx context.Context, cli *client.Client, id string, opts client.ContainerInspectOptions) (container.InspectResponse, error) {
backend/internal/services/container_service.go
  313: [in (*ContainerService).RedeployContainer L303-421] 	containerJSON, err := libarcane.ContainerInspectWithCompatibility(ctx, dockerClient, containerID, client.ContainerInspectOptions{})
  429: [in (*ContainerService).GetContainerByID L423-436] 	containerInspect, err := libarcane.ContainerInspectWithCompatibility(ctx, dockerClient, id, client.ContainerInspectOptions{})
  ...
      	[ast-shell] 20/74 matches shown, defs first, tests last; omitted: 13 src, 41 test
```

Agent 下一步可以根据标注的区间直接写 `sed -n '303,421p' backend/internal/services/container_service.go`。这个区间恰好等于符号边界，O1 不会改动它。

### A.3 AutoSkeleton：大文件整读

Agent 的命令：

```bash
cd /app && cat backend/internal/services/settings_service.go
```

文件 960 行、约 38000 字符，超过 6000 → 规则 A 触发。最近 grep 命中了 `GetBoolSetting` 所在的第 885 行。

`cat` 不带行号，所以输出用锚点格式（节选，右侧注释是 Agent 推算出的行号，不出现在输出里）：

```
      	[ast-shell] outline: bodies folded
package services                                          # L1

import (                                                  # L3
   ...
type SettingsService struct {                             # L40
   ...（字段原样，≤ 30 行）
}                                                         # L44
func NewSettingsService(ctx context.Context, db *database.DB) (*SettingsService, error) {
      	[ast-shell] L46-65: body folded
}                                                         # L66
   ...
func (s *SettingsService) GetBoolSetting(ctx context.Context, key string, defaultValue bool) bool {
	cfg := s.GetSettingsConfig()                          # L884
   ...（该函数含 hits，完整展开）
}                                                         # L895
```

如果是 `cat -n`，则每个代码行都带 `%6d\t` 行号，骨架丢弃空行，首行占位为 `outline: bodies folded, line numbers real`。

### A.4 不触发时：逐字节等于原样输出

```bash
sed -n '212,240p' modules/caddyhttp/celmatcher.go
```

区间恰好等于 `UnmarshalCaddyfile` 的边界，没有任何规则触发，输出与原命令完全一致（不加行号）。

---

## 附录 B：DeepSWE 轨迹统计汇总与口径

数据目录：`C:\Users\Administrator\Desktop\DeepSWE\trajectories`，共 28,457 条轨迹。覆盖 25 个以上模型（包括 claude-opus-5、gpt-5.6 系列、deepseek-v4、gemini-3.x、kimi-k2.7、glm-5.x、grok-4.x、qwen3.8 等），智能体为 mini-swe-agent 2.3.1–2.4.6。

| 指标 | 数值 |
|---|---|
| 单次输出字符占比（按命令类别） | cat/head/tail/nl 58.0%、sed -n 48.3%、grep/rg 24.9% |
| 按剩余轮数加权的累计上下文占比 | 上述三类合计 > 75%；测试运行 3.9% |
| `sed -n` 区间读取中重复读同一文件 | 54.9%（1924/3505） |
| 与历史区间重叠 | 33.8%（1186 次） |
| 同一文件整文件 cat 重复 | 39.1%（752/1920） |
| grep/rg 带 `\| head` | 54.2%（2438/4500） |
| 复合命令（含 `&&` 或 `;`） | 70.0% 的轮次 |
| `sed -n` 窗口大小中位数 | 70 行 |
| 触发 10000 字符截断的 observation | 8.5% |
| 手工用 grep 列定义（100 条轨迹） | 521 次 |
| 截断格式 | 头 5000 + 尾 5000 字符，`warning = "Output too long."`（实测） |
| 缓存命中比例（单条示例） | 输入约 44.0M token 中缓存命中约 42.7M（约 97%），212 步，$4.89 |

**口径说明**：
- 以上统计来自几十到几百条轨迹的抽样，用正则匹配命令文本得到，是近似值。复合命令会被同时计入多个类别，因此各类占比之和超过 100%；"所在 action 的输出"把整条复合命令的输出都算给其中出现的每一类命令。
- 以下数字在设计时有用，但尚未正式统计，需要补算：窗口大小的完整分位数（用于校准 `snap_up_max`/`snap_down_max`）、整文件读取的字符数分布（用于校准 `skeleton_min_chars`）、`nl -ba | sed -n` 与简单 `sed -n` 各自的占比、各语言的精确占比。
- 正式写论文时，应在 18.3 的拆分器完成之后，按子命令级别对全部 28,457 条轨迹重新统计，替换本表中的抽样数字。
