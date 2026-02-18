# Stage 1 四轮 AI 对话 Prompt 模板

---

## round1_system

你是一位学术级文本分析专家。请严格以 JSON 格式输出。

## round1_user

你是一位全书通读专家。请阅读以下书籍的章节概览，然后：

1. 撰写一段 500 字左右的「全书脉络」(synopsis)，概括全书的核心论点、结构逻辑和知识体系。要求：
   - 不要逐章罗列，而是提炼全书的内在逻辑线索
   - 指出本书的知识领域和目标读者
   - 标注本书的独特价值和核心贡献
2. 给出处理策略建议（JSON 格式）

章节概览：
{overview}

请按以下 JSON 格式输出：
```json
{{
  "synopsis": "全书脉络文本...",
  "strategy": {{
    "temperature": 0.2,
    "template_name": "full",
    "special_instructions": "（如有需要补充的特殊指令）",
    "skip_round2": false
  }}
}}
```

---

## round2_system

你是一位学术级文本分析专家。请严格以 JSON 格式输出，key 为章节序号。

## round2_user

基于全书脉络和章节概览，为每个章节生成元数据标注。

全书脉络：
{synopsis}

章节列表：
{unit_list}

章节概览：
{overview}

对每章输出：
- theme: 主题标签（2-5个词）
- difficulty: 难度等级（basic/intermediate/advanced）
- chapter_type: 章节类型
  - "preface": 序言/目录/前言/后记/致谢
  - "short": 内容极短（< 2000字）的章节
  - "normal": 正常章节
  - "long": 超长章节（> 15000字）
- summary: 100字以内的摘要（概括核心知识点，不要泛泛而谈）
- key_concepts: 本章的核心概念列表（3-8个）
- related_chapters: 关联章节序号列表

请按以下 JSON 格式输出：
```json
{{
  "01": {{
    "theme": "主题标签",
    "difficulty": "basic/intermediate/advanced",
    "chapter_type": "preface/short/normal/long",
    "summary": "100字以内摘要",
    "key_concepts": ["概念1", "概念2"],
    "related_chapters": ["02", "05"]
  }}
}}
```

---

## round3_system

你是一位学术级知识图谱专家。请严格以 JSON 格式输出。

## round3_user

基于全书脉络和章节概览，构建全书概念词典。

全书脉络：
{synopsis}

各章节标注：
{metas_summary}

请从全书中提取最核心的概念（最多50个），构建概念词典。
对每个概念：
- key 为概念名（如出现在多章则取最常见的叫法）
- value 为 50 字以内的精准定义（不要抄百科，要结合本书语境）

请只提取真正核心的概念，不要凑数。如果全书概念不多，10-20个就够了。

请输出一个 JSON 对象：
```json
{{
  "概念名": "50字以内定义"
}}
```

---

## round4_system

你是一位学术级知识图谱专家。请严格以 JSON 数组格式输出。

## round4_user

基于全书脉络和章节概览，识别章节间的关联关系。

全书脉络：
{synopsis}

各章节标注：
{metas_summary}

关联类型包括：
- "前置知识": A章是B章的前置知识
- "深化": B章是A章的深入展开
- "对比": A章和B章形成对比
- "应用": B章是A章理论的实践应用
- "补充": B章补充了A章未涉及的内容

请输出一个 JSON 数组，每个元素格式：
```json
{{"from": "01", "to": "03", "relation": "递进/因果/对比/补充", "description": "简述关联"}}
```

只输出 JSON 数组，最多 30 条。
