# 柏讯数据联邦（finddata-business）线的授权与凭据证据

本文件是 `line-manifest.json` 被剥离的自由文本的**仓内**留存。原因：清单会随
`/.well-known/line-manifest.json` **匿名公开**，而规格（`openspec` 的
`line-manifest` 能力）规定清单字符串键只能落在一个闭集里——闭集之外的自由文本
（典型是 `note`）一律判违规：一段自由文本不是「写短点」就安全，它压根不该出现在
公开契约里。下列内容含内部令牌名、模块与行号、验签参数、计量戳的实现位置，故只留在
仓库内，不进公开载荷。

对外可见的那部分是产品语义：柏讯数据联邦 MCP——核心读取（指标点值/序列/语义检索/
图谱）+ 九源域联邦（年鉴/法规/法律语料/指导案例/裁判文书/企查/世行/GTA/城市）+
DAAS 域，全只读；这是柏讯对客户的主数据面。

## 被剥离的 note（原文逐字，按清单路径索引）

### `<root>`

柏讯数据联邦 MCP：核心读取（指标点值/序列/语义检索/图谱）+ 九源域联邦（年鉴/法规/法律语料/指导案例/裁判文书/企查/世行/GTA/城市）+ DAAS 域，全只读。**这是柏讯对客户的主数据面**：门已有（Logto JWT + 服务间共享密钥），剩下的是「客户级钥匙」——今天客户只能同租户登录后经平台去用，词元钥匙还没通。`surfaces.account=false`：无本人用量/账单工具（账单在 token.finddatatech.cloud 读）。

### `identity`

本仓有**真门**，不是零校验：`FastMCP(..., auth=_build_auth())`（server.py:139）。门是 Logto JWT——`FDBIZ_JWKS_URI` 设了才开，未设则整条门不存在（server.py:30-32，本地开发/冒烟态），算法默认 ES384（`FDBIZ_ALGORITHM`），issuer/audience 可选（server.py:41-47）。另有服务间共享密钥 `FDBIZ_INTERNAL_TOKEN` 被当作**独立 bearer** 接受（`_InternalAwareVerifier`，恒时比较，server.py:39-60）——但它在 JWT 门之上叠加，只设 internal token 而不设 jwks 时门根本不挂。无断言兑换端点，jit_policy=off。

### `auth_tracks[]`

线自有共享密钥 `FDBIZ_INTERNAL_TOKEN`（server.py:39-60）：持有它就能直接消费本线，不经过平台，故成立为回退轨。三处限制必须一起读：(a) 只在 `FDBIZ_JWKS_URI` 同时设置时生效——`_build_auth()` 在 jwks 为空时直接返回 None（server.py:30-32），所以「只配内部令牌」等于没有门；(b) 它解析出的身份是**写死的** `fd-open-data-mcp-federation`（server.py:53-58），代表「联邦那一个服务」，不承载客户身份；(c) 全客户共用一把。为 fd-open-data-mcp 的透明联邦读而生（其 `FD_MCP_BUSINESS_TOKEN`），不是给终端客户的钥匙。

### `auth_tracks[]`

Logto 签发的 JWT 直接打本线（`JWTVerifier`，server.py:41-47）：同租户成员可脱离壹座、用自己 IdP 的令牌消费。这是本线今天对**人**的常命轨，与服务密钥互补——服务轨解机器消费，本轨解客户级身份。

### `auth_tracks[]`

目标态：客户拿词元钥匙直打本线。今天本仓不认 `sk-`——非内部令牌一律走 JWKS 验签，`sk-` 必然按无效令箭拒收。

### `billing`

本仓只做平台计量契约的**戳**（把 `rows_returned` 盖到读工具信封上，server.py:166-169），扣费由 registry/词元平面按此戳记；仓内零 sub2api 代码。
