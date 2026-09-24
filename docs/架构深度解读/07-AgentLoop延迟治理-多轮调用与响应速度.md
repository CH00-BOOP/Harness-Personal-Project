# 07 · Agent Loop 延迟治理：每轮都调 LLM，会不会拖慢客服回复？

> 04 篇讲透了 Agent Loop「想 → 决定调不调工具 → 看结果 → 再想 → 直到能答」的受控小循环。一个自然的追问随之而来——**我们是客服类 Agent，客户对响应速度很敏感；而 loop 每转一圈就调一次大模型，这会不会把回复拖慢？**
>
> 一句话结论：**会。在这套架构里，一次对话的延迟几乎等于「LLM 调用次数 × 单次 LLM 耗时」，相比传统「一问一答」慢 2~4 倍，对客服是真痛点。但正确的应对不是砍掉 loop，而是「压次数 + 优体感」——优先上流式输出与第一轮 recall 预取，投入小、收益最大。**

---

## 一、先定位延迟到底花在哪：LLM 是绝对大头

在开始优化前，必须先看清「一次对话的耗时构成」。把 04 篇的循环拆到耗时维度看，每一圈只有两类操作：

| 环节 | 是什么 | 量级 |
|------|--------|------|
| **工具执行** | `recall_playbook` / `course_search` / `student_cases` 走本地 embedding + SQLite；`handoff` 写本地库 | **毫秒级**（本地检索，可忽略） |
| **LLM 调用** | 每圈 `self.llm.chat(messages, schemas)` 一次网络往返 + 模型推理 | **秒级**（绝对大头） |

代码里也印证了这一点——`loop.py` 每转一圈都实打实调一次大模型：

```55:70:ai-customer-service/app/agent/loop.py
        while step < config.MAX_LOOP_STEPS:
            if time.time() - started > config.LOOP_TIMEOUT_SECONDS:
                ...
            step += 1
            schemas = self._tool_schemas()
            ...
            decision = self.llm.chat(messages, schemas)
```

**结论：一次对话的延迟 ≈ LLM 调用次数 × 单次 LLM 延迟。工具那点耗时可以忽略不计。** 所以「加不加速」的问题，本质是「能不能减少 LLM 往返次数」和「能不能优化每次往返的体感」。

---

## 二、算一笔账：一次客服对话要调几次 LLM？

回看 04 篇「转行做开发能就业不」那个例子，一次对话跑了 **3 圈**：

```text
─── 第 1 圈 ───  tool_call → recall_playbook（取套路样本）
─── 第 2 圈 ───  tool_call → student_cases（取同背景案例）
─── 第 3 圈 ───  final    → 产出最终回复
```

也就是 **3 次 LLM 调用**。而传统「一问一答」只有 **1 次**。

所以客户的体感延迟 ≈ 单次 LLM 延迟 × 2~4（典型区间）。**慢 2~4 倍，这不是杞人忧天，是实打实的体验差距。** 对客服这种「答得慢就流失」的场景，必须正视。

> 为什么不能靠砍 loop 来解决？因为多轮换来的是「先查后答、按套路应对、绝不乱承诺（包就业/分期）」的质量。客服**答错的代价**（乱承诺引发投诉、退费、纠纷）远高于慢一两秒。所以方向不是「为快砍质量」，而是「保住质量的前提下压延迟」。

---

## 三、优化清单（按性价比从高到低）

### 1. 流式输出：体感优化的天花板（优先做）

客户等待的真正痛点，是「首字迟迟不出」——盯着一个「对方正在输入…」干等。

- 最后一轮 `final` 用**流式（stream）** 吐字，**首 Token 时间（TTFT）** 决定体感。哪怕总耗时不变，「秒出首字 + 逐字滚动」的体验也远胜「憋 5 秒甩一大段」。
- 中间的决策轮（只输出 `tool_call`）**不需要**给客户看，只在 `final` 轮开流式即可。

现状：`LLMProvider.chat()` 是一次性返回 `LLMDecision`，尚无 stream 接口——这是最该第一个补的能力。

```39:43:ai-customer-service/app/llm/base.py
class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMDecision:
        """根据对话历史(含已执行的工具结果)与可用工具,决定下一步。"""
        raise NotImplementedError
```

### 2. 第一轮 recall 预取：省掉一次 LLM 往返（最值得做的结构性优化）

`chat_service.py` 的工作流程第 1 条是**铁律**：

```52:53:ai-customer-service/app/services/chat_service.py
            "工作流程(务必遵守):\n"
            "1. 回答任何客户咨询前,【必须先调用 recall_playbook】拿到老师教过的全部示范样本。\n"
```

既然 `recall_playbook` 是**无条件必调**的，那就**没必要花一次 LLM 往返让模型「决定」去调它**——纯属浪费一个来回。

做法：在进 loop 前，直接用当前用户问句预调用 `recall_playbook`，把套路样本**预填进上下文**，模型第一次开口时就已经带着资料。这样稳定砍掉 1 圈，通常能把典型的 3 圈降到 2 圈。**确定性的步骤不该花一次 LLM 决策。**

### 3. 鼓励一步并行点多个工具

`loop.py` 其实**已经支持**一步返回多个 `tool_calls` 并逐个执行：

```91:100:ai-customer-service/app/agent/loop.py
            # tool_calls:模型本步可请求一个或多个工具(并行);按标准
            # assistant(发起 tool_calls)→ tool(带 tool_call_id 回填)结构记录
            calls = decision.tool_calls
            messages.append({
                "role": "assistant",
                ...
            })
            for call in calls:
```

但当前提示词引导模型「分步走」（先套路、再案例）。可在提示词里鼓励「信息充分时，一次把需要的工具都点了」，让 `recall_playbook + course_search` 并行召回，减少串行圈数。

### 4. 决策轮与回复轮用不同档位的模型/参数

中间「决策轮」只需输出一个 `tool_call`（几十 token），完全可以用**更小更快的模型 + 更低 `max_tokens`**；只有 `final` 轮用好模型写长回复。差异化能明显压中间轮耗时与成本。

### 5. Prompt Caching：降 TTFT 又省钱

系统提示词很长且**每轮常驻**（技术流程 + 铁律 + 总纲，见 06 篇的「常驻税」）。若 ARK/豆包支持 prompt caching，把这段固定前缀缓存下来，可同时降低 TTFT 和 token 成本。

### 6. 过程可视化：缓解等待焦虑

项目本就全程记 `trace`（04 篇第五节）。前端可把「正在查询套路库…」「正在匹配案例…」实时展示，把「黑盒等待」变成「看得见的进度」——主观等待感会明显下降。这是 0 延迟成本的体感优化。

---

## 四、优化前后对比

| 维度 | 优化前（现状） | 优化后（上 #1 + #2） |
|------|----------------|----------------------|
| 典型 LLM 往返 | 3 次（recall → cases → final） | 2 次（预取 recall，剩 cases → final 流式） |
| 首字体感 | 憋满整段才出（TTFT 高） | final 轮流式，秒出首字 |
| 质量 | 先查后答、按套路、不乱承诺 | **不变**（保住） |
| 改动成本 | — | 小：加 stream 接口 + loop 前预取 |

---

## 五、一句话回答本篇的核心问题

| 问题 | 回答 |
|------|------|
| **每轮都调 LLM 会拖慢回复吗？** | 会。延迟 ≈ LLM 次数 × 单次耗时，典型 2~4 次，比一问一答慢 2~4 倍。 |
| **延迟花在哪？** | 几乎全在 LLM 往返；本地工具检索毫秒级可忽略。 |
| **该砍 loop 换速度吗？** | 不该。多轮换来的「不乱承诺」质量，对客服比快一两秒更重要。 |
| **性价比最高的两招？** | ① 流式输出（体感天花板）；② 第一轮 recall 预取（砍一次往返）。 |
| **目标状态？** | 把典型 3 圈压到「2 次 LLM + 流式首字」，质量不降、体感大升。 |
