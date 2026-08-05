<h1 align="center">Paper-BibChecker</h1>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-v0.1.0-blue?style=for-the-badge&color=2563EB" />
  <img alt="status" src="https://img.shields.io/badge/status-building-success?style=for-the-badge&color=16A34A" />
  <img alt="PRs" src="https://img.shields.io/badge/PRs-welcome-orange?style=for-the-badge&color=F97316" />
  <img alt="stars" src="https://img.shields.io/github/stars/RyanLiu112/Paper-BibChecker?style=for-the-badge&color=FBBF24" />
  
</p>

<p align="center">
  <b>面向论文投稿的 BibTeX 引用核验工具</b>
</p>

<p align="center">
  自动检索多个学术数据源，辅助核验文献是否真实存在，并发现错误的</br>
  标题、作者、年份、会议或期刊、DOI 与 arXiv ID。
</p>

<p align="center">
  <a href="#quick-start">快速开始</a> ·
  <a href="#results">检查结果</a> ·
  <a href="#how-it-works">工作原理</a> ·
  <a href="#organizers">组织者列表</a> ·
  <a href="#disclaimer">免责声明</a> ·
  <a href="#todo">TODO</a>
</p>

---

<a id="quick-start"></a>

## 🚀 快速开始

### 1. 安装

需要 **Python 3.10 或更高版本**。

```bash
git clone https://github.com/RyanLiu112/Paper-BibChecker.git
cd Paper-BibChecker
python -m pip install -e .
```

### 2. 可选配置

工具可以直接运行；如果需要启用 Semantic Scholar，或更稳定地访问 OpenAlex、Crossref、GitHub，可复制示例环境变量文件并填入本地凭据：

```bash
cp .env.example .env
```

常用配置：

```bash
# Semantic Scholar Academic Graph API key；未配置时默认不启用 S2 数据源
SEMANTIC_SCHOLAR_API_KEY=your_s2_api_key

# 也支持 Semantic Scholar 常见别名
S2_API_KEY=your_s2_api_key

# OpenAlex/Crossref polite pool 联系邮箱
OPENALEX_EMAIL=you@example.com
CROSSREF_EMAIL=you@example.com
```

> `.env` 只用于本地运行，已在 `.gitignore` 中忽略；不要提交真实 API key。未配置 Semantic Scholar key 时，S2 数据源不会加入默认检查流程；配置 key 后如 S2 返回 403、429 或超时，会作为该数据源错误记录在日志中，其他数据源仍会继续参与检查。

### 3. 使用

#### 方式一：只提供 `.bib` 文件

直接检查 BibTeX 文件中的全部条目：

```bash
bibchecker references.bib
```

**示例：**

仓库在 `examples/example_ref.bib` 中提供了一个包含 32 条引用的示例，其中包括 10+ 篇被 ICLR 2026 desk reject 的幻觉参考文献，也包括一定比例无幻觉、需核对的参考文献。

```text
examples/example_ref.bib
```

可以直接运行以下命令，体验工具如何定位标题、作者、年份、会议或期刊、DOI 及 arXiv ID 的问题：

```bash
# 在仓库根目录执行
bibchecker examples/example_ref.bib \
  --timeout 15 \
  --batch-size 4
```

运行后会在 `examples/` 目录下生成：

```text
examples/example_ref.log
examples/example_ref_issues.json
```

其中 `.log` 保存完整运行日志；`_issues.json` 只保留每条问题引用的 Bib key 和具体问题，方便直接阅读和后续处理。

在一次示例运行中，32 条条目得到如下结果：

| 状态 | 数量 |
| --- | ---: |
| ✅ 通过 | 3 |
| ⚠️ 信息需核对 | 7 |
| ❓ 无法确认 | 3 |
| ❌ 疑似幻觉 | 19 |
| **合计** | **32** |

> 这里的“未通过”是指原始检查结果中记录的所有非 `validated` 条目；示例文件不会自动修正这些引用，也不包含用于识别原论文的信息。

#### 方式二：同时提供 `.bib` 和 `.tex` 文件

只检查论文中实际引用的条目：

```bash
bibchecker references.bib --tex paper.tex
```

工具会递归读取 `paper.tex` 中通过 `\input` 和 `\include` 引入的 TeX 文件，并报告 **TeX 已引用但 Bib 中缺失** 的引用键。

如果还希望检查 Bib 中未被论文引用的条目：

```bash
bibchecker references.bib --tex paper.tex --check-unused
```

> 每次运行都会在 Bib 文件旁生成完整日志和待复核问题清单：
>
> ```text
> references.log
> references_issues.json
> ```

问题 JSON 采用精简格式，不保存候选论文、完整 Bib 字段或数据源原始响应：

```json
{
  "key": "example2025paper",
  "problems": [
    "标题与检索结果不一致",
    "作者列表存在差异"
  ]
}
```

<details>
<summary><b>更多常用选项</b></summary>

<br/>

```bash
# 输出完整 JSON 报告
bibchecker references.bib --tex paper.tex --json > report.json

# 网络较慢时增加超时时间
bibchecker references.bib --tex paper.tex --timeout 20

# 调整并行检查数量（默认为 4）
bibchecker references.bib --tex paper.tex --batch-size 8

# 查看全部命令
bibchecker --help
```

</details>

---

<a id="disclaimer"></a>

## ⚠️ 免责声明

<details open>
<summary><b>使用前请阅读</b></summary>

<br/>

Paper-BibChecker 只是一个辅助核验工具，检查结果受外部数据源可用性、元数据完整性以及网页结构变化等因素影响，可能出现漏报、误报或暂时无法确认的情况。

本工具输出的“通过”“信息需核对”“无法确认”和“疑似幻觉”均不构成对文献真实性或引用正确性的最终认定。尤其是“疑似幻觉”条目，**请勿仅依据自动检查结果直接删除或替换**。

在投稿、返修或正式发布论文前，建议作者逐条人工核对重要引用，并优先参考论文原文、出版社页面、会议或期刊官方论文集、DOI 页面及 arXiv 页面。使用者应自行判断并承担最终引用内容的核验责任。

</details>

<a id="features"></a>

## 🔍 它能检查什么？

Paper-BibChecker 会对 BibTeX 中的以下信息进行交叉核验：

- 文献是否能够在可信数据源中找到；
- 标题和作者是否对应同一篇文献；
- 年份、会议或期刊信息是否准确；
- DOI 或 arXiv ID 是否指向正确文献；
- 作者是否存在增删或顺序变化；
- TeX 中使用的引用键是否在 Bib 中存在。

工具只生成检查报告，**不会自动修改或删除你的 BibTeX 条目**。

<a id="results"></a>

## 📊 检查结果

每条引用会被归入以下四类：

| 状态 | 含义 | 建议 |
| --- | --- | --- |
| ✅ **通过** | 当前可比较字段未发现明显冲突 | 建议投稿前再抽查一次 |
| ⚠️ **信息需核对** | 已找到相关记录，但部分引用信息存在差异 | 人工判断差异的影响，并按需修正 |
| ❓ **无法确认** | 现有证据不足，或外部数据源信息不完整 | 需要人工核验 |
| ❌ **疑似幻觉** | 标识符冲突，或多个可靠来源均未找到可信记录 | 优先人工复核，切勿直接删除 |

示例：

```text
[⚠️ 信息需核对]
  结论：找到可信论文，但存在字段差异
  差异：
    • 年份：Bib=2024；检索=2023
    • 作者新增：New Author（检索第4位）
```

<a id="how-it-works"></a>

## 🧭 工作原理

检查器优先使用稳定、直接的证据，再进行标题检索：

1. 访问 Bib 中提供的论文页面、GitHub 或其他显式 URL；
2. 精确解析 DOI 与 arXiv ID；
3. 检索对应会议或期刊的官方论文集；
4. 通过 arXiv、OpenAlex、Crossref、DBLP、OpenReview 以及可选的 Semantic Scholar 等学术数据源交叉核验；
5. 综合比较标题、作者、年份、会议或期刊及标识符后给出保守判断。

当前已覆盖 ICLR、NeurIPS、ICML、ACL、EMNLP、CVPR、ICCV、ECCV、COLM、TACL、JMLR 等常见会议和期刊的官方或专用来源。

我们有意采用较保守的判断策略：**数据源超时、限流或暂时不可用，不会被直接视为“文献不存在”**。

<a id="organizers"></a>

## 👥 组织者列表

<p align="left">
  <b>感谢以下成员对本项目进行组织、开发与维护</b>
</p>

<p align="left">
  <a href="https://github.com/RyanLiu112"><img src="https://images.weserv.nl/?url=github.com/RyanLiu112.png?v=4&mask=circle" width="80" alt="RyanLiu112"></a>
   &nbsp;
  <a href="https://github.com/Lee1003-lee"><img src="https://images.weserv.nl/?url=github.com/Lee1003-lee.png?v=4&mask=circle" width="80" alt="Lee1003-lee"></a>
   &nbsp;
  <a href="https://github.com/ling-pan"><img src="https://images.weserv.nl/?url=github.com/ling-pan.png?v=4&mask=circle" width="80" alt="ling-pan"></a>
  &nbsp;&nbsp;
  <a href="https://github.com/qinlibo-hit"><img src="https://images.weserv.nl/?url=github.com/qinlibo-hit.png?v=4&mask=circle" width="80" alt="qinlibo-hit"></a>
    <a href="https://github.com/GoatCsu"><img src="https://images.weserv.nl/?url=https://avatars.githubusercontent.com/u/183998412?v=4&mask=circle" width="80" alt="gaote"></a>
  
</p>

---

<a id="todo"></a>

## 🗺️ TODO

- [ ] **支持直接输入 PDF，自动提取并检查参考文献**
- [ ] 提供更便捷的在线使用方式
- [ ] 持续扩展会议、期刊及学术数据源

## 🤝 欢迎贡献

欢迎通过 Issue 或 Pull Request：

- 反馈误报、漏报及数据源失效问题；
- 补充新的会议、期刊或学术数据源；
- 改进 BibTeX、LaTeX 与 PDF 解析能力；
- 完善文档、测试和使用体验。

如果这个项目对你有帮助，欢迎点一个 ⭐，也欢迎分享给有参考文献核验需求的同学。
