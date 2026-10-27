# 票面质量可疑条目

这些条目在票面侧有 OCR/引号/结构问题；P2 matcher 应把受影响字段标为 UNVERIFIABLE，而不是直接判操作人错。

| 原文 | family | flags | KIR |
|---|---|---|---|
| 停110kV电压开倒把手由”开刘”场至”碍倒”位置 | TRANSITION | OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:停110kv电压开倒把手 suf=把手 v=110kv q=停/电压开倒 \| TR=开刘->碍倒(切至) \| Q!=OCR_SUSPECT,QUOTE_IMBALANCE |
| 停用1KLP9母联310分刘压板 | EXECUTE | OCR_SUSPECT | EXECUTE \| A=停用:EXEC \| T=DEVICE:1klp9母联310分裂压板 suf=压板 id=1klp9 int=母联 q=310分裂 \| Q!=OCR_SUSPECT |
| 停用后台机35kV母线保护分园、电流差动软压板 | EXECUTE | OCR_SUSPECT | EXECUTE \| A=停用:EXEC \| T=PLACE:后台机 suf=后台机 place=后台机 ; OBJECT:母线保护 suf=母线保护 v=35kv q=分图 loc=分图 ; DEVICE:电流差动软压板 suf=软压板 q=电流差动 \| Q!=OCR_SUSPECT |
| 停用后台机35kV母线保护分留母联分测软压板 | EXECUTE | OCR_SUSPECT | EXECUTE \| A=停用:EXEC \| T=DEVICE:后台机35kv母线保护分裂母联分测软压板 suf=软压板 v=35kv place=后台机 q=母线保护分裂母联分测 \| Q!=OCR_SUSPECT |
| 内 | OTHER | FRAGMENT,NO_ACTION | OTHER \| Q!=FRAGMENT,NO_ACTION |
| 在#2主变10kV侧002开关柜电缆室母线排装设#（1）接地线一组 | EXECUTE | HASH_PAREN | EXECUTE \| A=装设:EXEC \| T=DEVICE:#2主变10kv侧002开关柜电缆室母线排 suf=母线排 id=002 v=10kv int=#2主变 place=开关柜/电缆室 q=侧 ; DEVICE:#(1)接地线 suf=接地线 id=1 place=开关柜/电缆室 q=# \| Q=1组 \| Q!=HASH_PAREN |
| 在35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车之间母排上装设#（8）接地 | EXECUTE | HASH_PAREN | EXECUTE \| A=装设:EXEC \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 ; DEVICE:35kv母线分段 2 320开关 suf=开关 id=320 v=35kv int=母线分段 2 ; DEVICE:35kv母线分段 2 320开关手车之间母排上 suf=母排 id=320 v=35kv int=母线分段 2 q=上 \| R=在35kv母线分段 2 320-2刀闸手车~35kv母线分段 2 320开关手车 \| Q!=HASH_PAREN |
| 在35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车立间母推上装投#(8)接地 | OTHER | HASH_PAREN,NO_ACTION | OTHER \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 q=刀闸 ; DEVICE:35kv母线分段 2 320开关手车 suf=手车 id=320 v=35kv int=母线分段 2 q=开关 \| Q!=HASH_PAREN,NO_ACTION |
| 地线一组 | OTHER | FRAGMENT,NO_ACTION | OTHER \| Q=1组 \| Q!=FRAGMENT,NO_ACTION |
| 将康发Ⅱ线0112开关操作方武地手由”远方”场至”就地”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:康发 2 线0112开关操作方式把手 suf=把手 id=0112 int=康发 2 线 q=开关操作方式 \| TR=远方->就地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将康发Ⅱ线0112开关操作方武护手由”跳地”场至”远方”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:康发 2 线0112开关操作方式把手 suf=把手 id=0112 int=康发 2 线 q=开关操作方式 \| TR=跳地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将流南Ⅱ线0125开关操作方武护手由”流地”场至”远方”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:流南 2 线0125开关操作方式把手 suf=把手 id=0125 int=流南 2 线 q=开关操作方式 \| TR=流地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将测控屏110kV桥100开关操作方武护手由”就地”场至”远方”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:测控屏110kv桥100开关操作方式把手 suf=把手 id=100 v=110kv int=桥 place=测控屏 q=开关操作方式 \| TR=就地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将测控屏110kV桥100开关操作方武护手由”远方”场至”就流地”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:测控屏110kv桥100开关操作方式把手 suf=把手 id=100 v=110kv int=桥 place=测控屏 q=开关操作方式 \| TR=远方->就流地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将测控屏35kV母线分段300开关操作方武地手电”就地”场至”远方”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=就地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将测控屏35kV母线分段300开关操作方武地手电”远方”场至”就地”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将测控屏35kV母线分段300开关操作方武护手由”就地”场至”远方”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=就地->远方(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 将测控屏35kV母线分段300开关操作方武护手由”远方”场至”就地”位置 | TRANSITION | OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | TRANSITION \| T=DEVICE:测控屏35kv母线分段300开关操作方式把手 suf=把手 id=300 v=35kv int=母线分段 place=测控屏 q=开关操作方式 \| TR=远方->就地(切至) \| Q!=OCR_SUSPECT,OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 投入后台机35kV母线保护分留母联分刘软压板 | EXECUTE | OCR_SUSPECT,OCR_SUSPECT | EXECUTE \| A=投入:EXEC \| T=DEVICE:后台机35kv母线保护分裂母联分裂软压板 suf=软压板 v=35kv place=后台机 q=母线保护分裂母联分裂 \| Q!=OCR_SUSPECT,OCR_SUSPECT |
| 投入后台机35kV母线保护分留电流差动软压板 | EXECUTE | OCR_SUSPECT | EXECUTE \| A=投入:EXEC \| T=DEVICE:后台机35kv母线保护分裂电流差动软压板 suf=软压板 v=35kv place=后台机 q=母线保护分裂电流差动 \| Q!=OCR_SUSPECT |
| 护测控屏内 | OTHER | NO_ACTION | OTHER \| T=PLACE:护测控屏内 suf=屏内 place=护测控屏内 \| Q!=NO_ACTION |
| 拆除 | EXECUTE | NO_TARGET | EXECUTE \| A=拆除:EXEC \| Q!=NO_TARGET |
| 检查#2主变及110kV侧102开关、35kV侧302开关、10kV侧002开关相关保护压板确在#2主变保 | VERIFY | INCOMPLETE_VERIFY | VERIFY \| A=检查:VERIFY \| T=DEVICE:#2主变及110kv侧102开关 suf=开关 id=102 v=110kv int=#2主变 q=侧 ; DEVICE:35kv侧302开关 suf=开关 id=302 v=35kv q=侧 ; DEVICE:10kv侧002开关相关保护压板 suf=保护压板 id=002 v=10kv q=侧/开关相关 ; DEVICE:#2主变 suf=主变 id=#2 \| Q!=INCOMPLETE_VERIFY |
| 检查110kV系统锁环后负荷转档指示正确 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=SCOPE:110kv系统 suf=系统 v=110kv scope=110kv系统 ; OBJECT:负荷转带指示 suf=负荷转带指示 q=锁环后 \| S=正确:condition \| Q!=OCR_SUSPECT |
| 检查35kV低预低压减载分留顺查压板确在停用位置 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv低预低压减载分裂顺查压板 suf=压板 v=35kv q=低预低压减载分裂顺查 \| S=停用位置:position \| Q!=OCR_SUSPECT |
| 检查35kV低预低压减载装置相关保护压板确在后台机35kV低预低压减载分留内 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv低预低压减载装置相关保护压板 suf=保护压板 v=35kv q=低预低压减载装置相关 ; PLACE:后台机35kv低预低压减载分裂内 suf=后台机 place=后台机35kv低预低压减载分裂内 \| S=在:position \| Q!=OCR_SUSPECT |
| 检查35kV母线保护屏查南压板场在停用位置 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv母线保护屏查南压板 suf=压板 v=35kv place=保护屏 q=母线/查南 \| S=停用位置:position \| Q!=OCR_SUSPECT |
| 检查35kV母线保护相关保护压庄板确在35kV母线保护屏内 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv母线保护相关保护压板 suf=保护压板 v=35kv q=母线保护相关 ; PLACE:35kv母线保护屏内 suf=屏内 v=35kv place=35kv母线保护屏内 \| S=在:position \| Q!=OCR_SUSPECT |
| 检查35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车之间母排上#8接地线确已 | VERIFY | INCOMPLETE_VERIFY | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 q=刀闸 ; DEVICE:35kv母线分段 2 320开关 suf=开关 id=320 v=35kv int=母线分段 2 ; DEVICE:35kv母线分段 2 320开关手车之间母排上#8接地线 suf=接地线 id=#8 v=35kv int=母线分段 2 q=320开关手车之间 loc=母排上 ; DEVICE:手车之间母排上 suf=母排 q=手车之间/上 \| Q!=INCOMPLETE_VERIFY |
| 检查35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车立间母推上开8接地线确已 | VERIFY | INCOMPLETE_VERIFY | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 q=刀闸 ; DEVICE:35kv母线分段 2 320开关 suf=开关 id=320 v=35kv int=母线分段 2 ; DEVICE:35kv母线分段 2 320开关手车立间母推上开8接地线 suf=接地线 id=8 v=35kv int=母线分段 2 q=320开关手车立间母推上开 \| Q!=INCOMPLETE_VERIFY |
| 检查35kV母线分段Ⅱ320-2刀闸手车至35kV母线分段Ⅱ320开关手车立间母推上确已装投开8接 | VERIFY | INCOMPLETE_VERIFY | VERIFY \| A=检查:VERIFY \| T=DEVICE:35kv母线分段 2 320-2刀闸手车 suf=手车 id=320-2 v=35kv int=母线分段 2 q=刀闸 ; DEVICE:35kv母线分段 2 320开关手车 suf=手车 id=320 v=35kv int=母线分段 2 q=开关 \| Q!=INCOMPLETE_VERIFY |
| 检查35kV系统符合确环条件 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=SCOPE:35kv系统 suf=系统 v=35kv scope=35kv系统 ; OBJECT:条件 suf=条件 \| S=符合:condition,合环:operating_state \| Q!=OCR_SUSPECT |
| 检查35kV系统顺环后负荷转档指示正确 | VERIFY | OCR_SUSPECT,OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=SCOPE:35kv系统 suf=系统 v=35kv scope=35kv系统 ; OBJECT:负荷转带指示 suf=负荷转带指示 \| S=合环:operating_state,正确:condition \| Q!=OCR_SUSPECT,OCR_SUSPECT |
| 检查后台机35kV母线保护分留顺查压板场在停用位置 | VERIFY | OCR_SUSPECT,OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=DEVICE:后台机35kv母线保护分裂顺查压板 suf=压板 v=35kv place=后台机 q=母线保护分裂顺查 \| S=停用位置:position \| Q!=OCR_SUSPECT,OCR_SUSPECT |
| 检查期1、#2主变10kV侧解列后负荷转移指示正确 | VERIFY | OCR_SUSPECT | VERIFY \| A=检查:VERIFY \| T=OBJECT:#2主变10kv侧解列后负荷转移指示 suf=负荷转移指示 v=10kv int=#1、#2主变 q=侧 \| S=解列:position,正确:condition \| Q!=OCR_SUSPECT |
| 检查流南Ⅱ线0125开关操作方武地手确在”就地”位置 | VERIFY | OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE | VERIFY \| A=检查:VERIFY \| T=DEVICE:流南 2 线0125开关操作方式把手 suf=把手 id=0125 int=流南 2 线 q=开关操作方式 \| S=就地:position \| Q!=OCR_SUSPECT,OCR_SUSPECT,QUOTE_IMBALANCE |
| 检查黄埋线312-3刀闸开关侧确已装设#（接地线一组 | VERIFY | HASH_PAREN | VERIFY \| A=检查:VERIFY \| T=DEVICE:黄埋线312-3刀闸开关侧 suf=刀闸 id=312-3 int=黄埋线 q=开关侧 ; DEVICE:#(接地线 suf=接地线 q=# \| S=装设:confirmed_action \| Q=1组 \| Q!=HASH_PAREN |
| 线一组 | OTHER | FRAGMENT,NO_ACTION | OTHER \| Q=1组 \| Q!=FRAGMENT,NO_ACTION |