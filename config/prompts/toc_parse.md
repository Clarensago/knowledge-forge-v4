以下是一本书的目录区域原文：

```
{toc_text}
```

请解析该目录，输出一个 JSON 对象，格式如下：

```json
{{
  "entries": [
    {{
      "level": 1,
      "title": "PART Ⅰ 作家和故事艺术",
      "children": [
        {{"level": 2, "title": "CHAPTER 01 故事问题", "children": []}}
      ]
    }}
  ]
}}
```

规则：
1. level=1 最粗（篇/卷/PART），level=2 中间（章/CHAPTER），level=3 更细（节/小节）
2. 单层结构所有条目 level=1，children=[]
3. 忽略前言/序言/目录/附录/版权
4. 保留原始标题
5. 只输出 JSON
