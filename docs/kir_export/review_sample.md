# KIR 人工抽查样本

从 944 条去重条目里分层抽样 77 条。逐条确认：动作、目标、编号、状态/位置、场所/范围、数量/区间是否齐全，有没有把非关键修饰误当关键事实。

| # | 原文 | KIR |
|---:|---|---|
| 1 | 停110kV电压开倒把手由”开刘”场至”碍倒”位置 | TRANSITION \| T=DEVICE:停110kv电压开倒把手 suf=把手 v=110kv q=停/电压开倒 \| TR=开刘->碍倒(切至) \| Q!=OCR_SUSPECT,QUOTE_IMBALANCE |
| 2 | 在顺控主机生成110kV桥100开关由热备用转运行操作票 | TRANSITION \| A=生成:EXEC \| T=DEVICE:110kv桥100开关 suf=开关 id=100 v=110kv int=桥 place=顺控主机 ; OBJECT:运行操作票 suf=操作票 \| TR=热备用->运行(转) |
| 3 | 将#2主变10kV侧002开关手车由试验位置拉至检修位置 | TRANSITION \| T=DEVICE:#2主变10kv侧002开关手车 suf=手车 id=002 v=10kv int=#2主变 q=侧/开关 \| TR=试验位置->检修位置(拉至) |
| 4 | 将#2主变10kV侧002开关操作方式把手由“远方”切至“就地”位置 | TRANSITION \| T=DEVICE:#2主变10kv侧002开关操作方式把手 suf=把手 id=002 v=10kv int=#2主变 q=侧/开关操作方式 \| TR=远方->就地(切至) |
| 5 | 将#2主变110kV侧2-D10中性点接地刀闸操作方式把手由“就地”切至“远方”位置 | TRANSITION \| T=DEVICE:#2主变110kv侧2-d10中性点接地刀闸操作方式把手 suf=把手 id=2-d10 v=110kv int=#2主变 q=侧/中性点接地刀闸操作方式 \| TR=就地->远方(切至) |
| 6 | 将35kV母线分段Ⅱ320-2刀闸手车由检修位置推至试验位置 | TRANSITION \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 q=刀闸 \| TR=检修位置->试验位置(推至) |
| 7 | 将公用测控及电压并列屏：电压并列把手由“分列”切至“并列”位置V | TRANSITION \| T=DEVICE:电压并列把手 suf=把手 place=公用测控及电压并列屏 q=电压并列 \| TR=分列->并列(切至) |
| 8 | 将冉固线3104开关手车由运行位置拉至试验位置 | TRANSITION \| T=DEVICE:冉固线3104开关手车 suf=手车 id=3104 int=冉固线 q=开关 \| TR=运行位置->试验位置(拉至) |
| 9 | 将康发Ⅱ线0112开关手车由试验位置推至运行位置 | TRANSITION \| T=DEVICE:康发 2 线0112开关手车 suf=手车 id=0112 int=康发 2 线 q=开关 \| TR=试验位置->运行位置(推至) |
| 10 | 将康发Ⅱ线0112开关操作方武护手由”跳地”场至”远方”位置 | TRANSITION \| T=DEVICE:康发 2 线0112开关操作方式把手 suf=把手 id=0112 int=康发 2 线 q=开关操作方式 \| TR=跳地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 11 | 将康复Ⅱ线0112开关操作方式把手由“就地”切至“远方”位置 | TRANSITION \| T=DEVICE:康复 2 线0112开关操作方式把手 suf=把手 id=0112 int=康复 2 线 q=开关操作方式 \| TR=就地->远方(切至) |
| 12 | 将流南Ⅱ线0125开关操作方武护手由”流地”场至”远方”位置 | TRANSITION \| T=DEVICE:流南 2 线0125开关操作方式把手 suf=把手 id=0125 int=流南 2 线 q=开关操作方式 \| TR=流地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 13 | 将测控屏#2主变10kV侧002开关操作方式把手由“就地”切至“远方”位置 | TRANSITION \| T=DEVICE:测控屏#2主变10kv侧002开关操作方式把手 suf=把手 id=002 v=10kv int=#2主变 place=测控屏 q=侧/开关操作方式 \| TR=就地->远方(切至) |
| 14 | 将测控屏#2主变110kV侧102开关操作方式把手由“远方”切至“就地”位置 | TRANSITION \| T=DEVICE:测控屏#2主变110kv侧102开关操作方式把手 suf=把手 id=102 v=110kv int=#2主变 place=测控屏 q=侧/开关操作方式 \| TR=远方->就地(切至) |
| 15 | 将测控屏#2主变35kV侧302开关操作方式把手由“就地”切至“远方”位置 | TRANSITION \| T=DEVICE:测控屏#2主变35kv侧302开关操作方式把手 suf=把手 id=302 v=35kv int=#2主变 place=测控屏 q=侧/开关操作方式 \| TR=就地->远方(切至) |
| 16 | 将测控屏110kV桥100开关操作方式把手由“远方”切至“就地”位置 | TRANSITION \| T=DEVICE:测控屏110kv桥100开关操作方式把手 suf=把手 id=100 v=110kv int=桥 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) |
| 17 | 将测控屏35kV母线分段300开关操作把手由“就地”切至“远方”位置 | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作 \| TR=就地->远方(切至) |
| 18 | 将测控屏35kV母线分段300开关操作方式把手由“远方”切至“就地”位置 | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) |
| 19 | 将测控屏35kV母线分段300开关操作方武护手由”就地”场至”远方”位置 | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=就地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 20 | 将测控屏仿河线111开关操作方式把手由“远方”切至“就地”位置 | TRANSITION \| T=DEVICE:测控屏仿河线111开关操作方式把手 suf=把手 id=111 int=仿河线 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) |
| 21 | 13检查110kV系统合环后负荷分配指示正确 | VERIFY \| A=检查:VERIFY \| T=SCOPE:110kv系统 suf=系统 v=110kv scope=110kv系统 ; OBJECT:指示 suf=指示 q=后负荷分配 \| S=合环:operating_state,正确:condition |
| 22 | 检查#2主变10kV侧002开关手车确已推至试验位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:#2主变10kv侧002开关手车 suf=手车 id=002 v=10kv int=#2主变 q=侧/开关 \| S=推至:confirmed_action,试验位置:position |
| 23 | 检查#2主变110kV侧102开关机械位置指示确在合闸位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:#2主变110kv侧102开关 suf=开关 id=102 v=110kv int=#2主变 q=侧 ; OBJECT:#2主变110kv侧102开关机械位置指示 suf=机械位置指示 id=102 v=110kv int=#2主变 q=侧/开关 \| S=合闸位置:position |
| 24 | 检查#2主变及110kV侧102开关、35kV侧302开关、10kV侧002开关相关保护压板确在#2主变保 | VERIFY \| A=检查:VERIFY \| T=DEVICE:#2主变及110kv侧102开关 suf=开关 id=102 v=110kv int=#2主变 q=侧 ; DEVICE:35kv侧302开关 suf=开关 id=302 v=35kv q=侧 ; DEVICE:10kv侧002开关相关保护压板 suf=保护压板 id=002 v=10kv q=侧/开关相关 ; DEVICE:#2主变 suf=主变 id=#2 \| Q!=INCOMPLETE_VERIFY |
| 25 | 检查10kV母线分段000开关电气位置指示确已合好 | VERIFY \| A=检查:VERIFY \| T=DEVICE:10kv母线分段000开关 suf=开关 id=000 v=10kv int=母线分段 ; OBJECT:10kv母线分段000开关电气位置指示 suf=电气位置指示 id=000 v=10kv int=母线分段 q=开关 \| S=合好:confirmed_action |
| 26 | 检查1CLP1差动保护跳102开关压板确在投入位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:1clp1差动保护跳102开关压板 suf=压板 id=1clp1 q=差动保护跳102开关 \| S=投入位置:position |
| 27 | 检查2-RLP2111仿河线差动保护投入压板确在投入位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:2-rlp2111仿河线差动保护投入压板 suf=压板 id=2-rlp2 int=仿河线 q=111/差动保护投入 \| S=投入位置:position |
| 28 | 检查2KLP935kV冉固线保护低压减载投入压板确在停用位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:2klp935kv冉固线保护低压减载投入压板 suf=压板 id=2klp9 v=35kv int=冉固线 q=保护低压减载投入 \| S=停用位置:position |
| 29 | 检查35kV母线分段1320-2刀闸手车确在检修位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv母线分段1320-2刀闸手车 suf=手车 id=1320-2 v=35kv int=母线分段 q=刀闸 \| S=检修位置:position |
| 30 | 检查6QLP2本体测控解除闭锁压板确在停用位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:6qlp2本体测控解除闭锁压板 suf=压板 id=6qlp2 q=本体测控解除闭锁 \| S=停用位置:position |
| 31 | 检查仿香线112-1刀闸机械位置指示确在分闸位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:仿香线112-1刀闸 suf=刀闸 id=112-1 int=仿香线 ; OBJECT:仿香线112-1刀闸机械位置指示 suf=机械位置指示 id=112-1 int=仿香线 q=刀闸 \| S=分闸位置:position |
| 32 | 检查仿香线118-1刀闸机械位置指示确已合好 | VERIFY \| A=检查:VERIFY \| T=DEVICE:仿香线118-1刀闸 suf=刀闸 id=118-1 int=仿香线 ; OBJECT:仿香线118-1刀闸机械位置指示 suf=机械位置指示 id=118-1 int=仿香线 q=刀闸 \| S=合好:confirmed_action |
| 33 | 检查冉固线3104开关机械位置指示确在分闸位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:冉固线3104开关 suf=开关 id=3104 int=冉固线 ; OBJECT:冉固线3104开关机械位置指示 suf=机械位置指示 id=3104 int=冉固线 q=开关 \| S=分闸位置:position |
| 34 | 检查投低压减载软压板确在停用位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:投低压减载软压板 suf=软压板 q=投低压减载 \| S=停用位置:position |
| 35 | 检查汇控柜#2主变110kV侧102-D1接地刀闸电气位置指示确已拉开 | VERIFY \| A=检查:VERIFY \| T=DEVICE:汇控柜#2主变110kv侧102-d1接地刀闸 suf=接地刀闸 id=102-d1 v=110kv int=#2主变 place=汇控柜 q=侧 ; OBJECT:汇控柜#2主变110kv侧102-d1接地刀闸电气位置指示 suf=电气位置指示 id=102-d1 v=110kv int=#2主变 place=汇控柜 q=侧/接地刀闸 \| S=拉开:confirmed_action |
| 36 | 检查汇控柜仿河线111开关电气位置指示确在合闸位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:汇控柜仿河线111开关 suf=开关 id=111 int=仿河线 place=汇控柜 ; OBJECT:汇控柜仿河线111开关电气位置指示 suf=电气位置指示 id=111 int=仿河线 place=汇控柜 q=开关 \| S=合闸位置:position |
| 37 | 检查汇控柜仿香线118-1刀闸电气位置指示确已拉开 | VERIFY \| A=检查:VERIFY \| T=DEVICE:汇控柜仿香线118-1刀闸 suf=刀闸 id=118-1 int=仿香线 place=汇控柜 ; OBJECT:汇控柜仿香线118-1刀闸电气位置指示 suf=电气位置指示 id=118-1 int=仿香线 place=汇控柜 q=刀闸 \| S=拉开:confirmed_action |
| 38 | 检查汇控柜菏铁Ⅰ线119-3刀闸电气位置指示确已合好 | VERIFY \| A=检查:VERIFY \| T=DEVICE:汇控柜菏铁 1 线119-3刀闸 suf=刀闸 id=119-3 int=菏铁 1 线 place=汇控柜 ; OBJECT:汇控柜菏铁 1 线119-3刀闸电气位置指示 suf=电气位置指示 id=119-3 int=菏铁 1 线 place=汇控柜 q=刀闸 \| S=合好:confirmed_action |
| 39 | 检查测控屏#2主变35kV侧302开关电气位置指示确已拉开 | VERIFY \| A=检查:VERIFY \| T=DEVICE:测控屏#2主变35kv侧302开关 suf=开关 id=302 v=35kv int=#2主变 place=测控屏 q=侧 ; OBJECT:测控屏#2主变35kv侧302开关电气位置指示 suf=电气位置指示 id=302 v=35kv int=#2主变 place=测控屏 q=侧/开关 \| S=拉开:confirmed_action |
| 40 | 检查菏铁Ⅰ线119-D3接地刀闸机械位置指示确在合闸位置 | VERIFY \| A=检查:VERIFY \| T=DEVICE:菏铁 1 线119-d3接地刀闸 suf=接地刀闸 id=119-d3 int=菏铁 1 线 ; OBJECT:菏铁 1 线119-d3接地刀闸机械位置指示 suf=机械位置指示 id=119-d3 int=菏铁 1 线 q=接地刀闸 \| S=合闸位置:position |
| 41 | 停用#2主变保护测控屏：2-2CLP6中后备保护跳300开关压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:2-2clp6中后备保护跳300开关压板 suf=压板 id=2-2clp6 place=#2主变保护测控屏 q=中后备保护跳300开关 |
| 42 | 停用1-31CLP8备投合112仿香线开关压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:1-31clp8备投合112仿香线开关压板 suf=压板 id=1-31clp8 int=仿香线 q=备投合112/开关 |
| 43 | 停用1C12LP13110开关跳闸出口压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:1c12lp13110开关跳闸出口压板 suf=跳闸出口压板 id=1c12lp13 q=110开关 |
| 44 | 停用1C7LP13105开关跳闸出口压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:1c7lp13105开关跳闸出口压板 suf=跳闸出口压板 id=1c7lp13 q=105开关 |
| 45 | 停用1CLP910KV备投合101开关压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:1clp910kv备投合101开关压板 suf=压板 id=1clp9 v=10kv q=备投合101开关 |
| 46 | 停用52LP273112开关跳闸出口压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:52lp273112开关跳闸出口压板 suf=跳闸出口压板 id=52lp273 q=112开关 |
| 47 | 合上#2主变110kV侧102-D1接地刀闸 | EXECUTE \| A=合上:EXEC \| T=DEVICE:#2主变110kv侧102-d1接地刀闸 suf=接地刀闸 id=102-d1 v=110kv int=#2主变 q=侧 |
| 48 | 合上110kV仿香线、#2PT间隔汇控柜PT二次侧0.2级空气开关三只 | EXECUTE \| A=合上:EXEC \| T=DEVICE:#2pt间隔汇控柜pt2次侧0.2级空气开关 suf=空气开关 id=#2pt int=110kv仿香线 place=汇控柜 q=间隔/pt2次侧0.2级 \| Q=3只 |
| 49 | 合上仿香线112开关控制电源空气开关V | EXECUTE \| A=合上:EXEC \| T=DEVICE:仿香线112开关控制电源空气开关 suf=空气开关 id=112 int=仿香线 q=开关控制电源 |
| 50 | 合上菏铁Ⅰ线119-3刀闸 | EXECUTE \| A=合上:EXEC \| T=DEVICE:菏铁 1 线119-3刀闸 suf=刀闸 id=119-3 int=菏铁 1 线 |
| 51 | 投入#2主变保护测控屏：5QLP1本体重瓦斯跳闸投入压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:5qlp1本体重瓦斯跳闸投入压板 suf=压板 id=5qlp1 place=#2主变保护测控屏 q=本体重瓦斯跳闸投入 |
| 52 | 投入10kV周保线0121开关柜1CLP10121开关保护跳闸出口压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:10kv周保线0121开关柜1clp10121开关保护跳闸出口压板 suf=跳闸出口压板 id=1clp10 v=10kv int=周保线 place=开关柜 q=0121/121开关保护 |
| 53 | 投入1C19LP13115开关跳闸出口压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:1c19lp13115开关跳闸出口压板 suf=跳闸出口压板 id=1c19lp13 q=115开关 |
| 54 | 投入1CLP1010kV备投合102开关压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:1clp1010kv备投合102开关压板 suf=压板 id=1clp10 v=10kv q=备投合102开关 |
| 55 | 投入1KLP9母联310分列压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:1klp9母联310分列压板 suf=压板 id=1klp9 int=母联 q=310分列 |
| 56 | 投入后台机35kV母线保护分留母联分刘软压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:后台机35kv母线保护分裂母联分裂软压板 suf=软压板 v=35kv place=后台机 q=母线保护分裂母联分裂 \| Q!=OCR_SUSPECT,OCR_SUSPECT |
| 57 | 拉开#2主变110kV侧102-D2接地刀闸 | EXECUTE \| A=拉开:EXEC \| T=DEVICE:#2主变110kv侧102-d2接地刀闸 suf=接地刀闸 id=102-d2 v=110kv int=#2主变 q=侧 |
| 58 | 拉开110kV#2PTPL12刀闸 | EXECUTE \| A=拉开:EXEC \| T=DEVICE:110kv#2ptpl12刀闸 suf=刀闸 id=#2ptpl12 v=110kv |
| 59 | 拉开仿香线112开关储能电源空气开关 | EXECUTE \| A=拉开:EXEC \| T=DEVICE:仿香线112开关储能电源空气开关 suf=空气开关 id=112 int=仿香线 q=开关储能电源 |
| 60 | 拉开菏铁Ⅰ线119-2刀闸 | EXECUTE \| A=拉开:EXEC \| T=DEVICE:菏铁 1 线119-2刀闸 suf=刀闸 id=119-2 int=菏铁 1 线 |
| 61 | 内 | OTHER \| Q!=FRAGMENT,NO_ACTION |
| 62 | 在35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车立间母推上装投#(8)接地 | OTHER \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 q=刀闸 ; DEVICE:35kv母线分段 2 320开关手车 suf=手车 id=320 v=35kv int=母线分段 2 q=开关 \| Q!=HASH_PAREN,NO_ACTION |
| 63 | 地线一组 | OTHER \| Q=1组 \| Q!=FRAGMENT,NO_ACTION |
| 64 | 护测控屏内 | OTHER \| T=PLACE:护测控屏内 suf=屏内 place=护测控屏内 \| Q!=NO_ACTION |
| 65 | 线一组 | OTHER \| Q=1组 \| Q!=FRAGMENT,NO_ACTION |
| 66 | 停用1KLP9母联310分刘压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:1klp9母联310分裂压板 suf=压板 id=1klp9 int=母联 q=310分裂 \| Q!=OCR_SUSPECT |
| 67 | 停用后台机35kV母线保护分园、电流差动软压板 | EXECUTE \| A=停用:EXEC \| T=PLACE:后台机 suf=后台机 place=后台机 ; OBJECT:母线保护 suf=母线保护 v=35kv q=分图 loc=分图 ; DEVICE:电流差动软压板 suf=软压板 q=电流差动 \| Q!=OCR_SUSPECT |
| 68 | 停用后台机35kV母线保护分留母联分测软压板 | EXECUTE \| A=停用:EXEC \| T=DEVICE:后台机35kv母线保护分裂母联分测软压板 suf=软压板 v=35kv place=后台机 q=母线保护分裂母联分测 \| Q!=OCR_SUSPECT |
| 69 | 在#2主变10kV侧002开关柜电缆室母线排装设#（1）接地线一组 | EXECUTE \| A=装设:EXEC \| T=DEVICE:#2主变10kv侧002开关柜电缆室母线排 suf=母线排 id=002 v=10kv int=#2主变 place=开关柜/电缆室 q=侧 ; DEVICE:#(1)接地线 suf=接地线 id=1 place=开关柜/电缆室 q=# \| Q=1组 \| Q!=HASH_PAREN |
| 70 | 在35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车之间母排上装设#（8）接地 | EXECUTE \| A=装设:EXEC \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 ; DEVICE:35kv母线分段 2 320开关 suf=开关 id=320 v=35kv int=母线分段 2 ; DEVICE:35kv母线分段 2 320开关手车之间母排上 suf=母排 id=320 v=35kv int=母线分段 2 q=上 \| R=在35kv母线分段 2 320-2刀闸手车~35kv母线分段 2 320开关手车 \| Q!=HASH_PAREN |
| 71 | 将康发Ⅱ线0112开关操作方武地手由”远方”场至”就地”位置 | TRANSITION \| T=DEVICE:康发 2 线0112开关操作方式把手 suf=把手 id=0112 int=康发 2 线 q=开关操作方式 \| TR=远方->就地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 72 | 将测控屏110kV桥100开关操作方武护手由”就地”场至”远方”位置 | TRANSITION \| T=DEVICE:测控屏110kv桥100开关操作方式把手 suf=把手 id=100 v=110kv int=桥 place=测控屏 q=开关操作方式 \| TR=就地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 73 | 将测控屏110kV桥100开关操作方武护手由”远方”场至”就流地”位置 | TRANSITION \| T=DEVICE:测控屏110kv桥100开关操作方式把手 suf=把手 id=100 v=110kv int=桥 place=测控屏 q=开关操作方式 \| TR=远方->就流地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 74 | 将测控屏35kV母线分段300开关操作方武地手电”就地”场至”远方”位置 | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=就地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 75 | 将测控屏35kV母线分段300开关操作方武地手电”远方”场至”就地”位置 | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 76 | 将测控屏35kV母线分段300开关操作方武护手由”远方”场至”就地”位置 | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 77 | 投入后台机35kV母线保护分留电流差动软压板 | EXECUTE \| A=投入:EXEC \| T=DEVICE:后台机35kv母线保护分裂电流差动软压板 suf=软压板 v=35kv place=后台机 q=母线保护分裂电流差动 \| Q!=OCR_SUSPECT |