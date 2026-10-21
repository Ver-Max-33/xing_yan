"""能拿你自己的数据检验的选股说法: each a single-condition test — the formula as an event
against random entries over the same range (spec §9.5). Where no number was given the threshold is a research
choice and says so; the formula can be changed before it runs. The results are references, never rules."""
CHECKS = [
    {"id": "vwap5", "label": "离均价线 5 个点以上（说法：次日容易低开）", "expr": "(C - VWAP) / REF(C, 1) >= 0.05",
     "look": "看“次日低开”的样本占比和随机入场比；日线上是收盘离当天均价 5 个点以上（原说法是盘中，尤其尾盘急拉）"},
    {"id": "long13", "label": "横盘蓄势粗筛（不要求涨停）：两个月振幅 < 30%、60 日线不下降、收在 60 日线上",
     "expr": "(HHV(H, 40) / LLV(L, 40) - 1) * 100 < 30 AND MA(C, 60) >= REF(MA(C, 60), 1) AND C > MA(C, 60)",
     "look": "看 20 日超额和胜率对随机入场：原说法是它只是减少看图的数量，Codex 用 300 只股票检验也没显出超额"},
    {"id": "vol2", "label": "爆大量：当天量是昨天的 2 倍以上（横盘蓄势选股器）", "expr": "VOL_MULT >= 2",
     "look": "原说法是这只用来粗筛异动、不是突破门槛：看它本身有没有超额"},
    {"id": "ma60", "label": "60 日线走平或上翘（黑马基因）", "expr": "MA_SLOPE_PCT(60, 5) >= 0",
     "look": "转平 / 转上的那天当事件；没定“多平算走平”，这里用 ≥ 0（研究取值）"},
    {"id": "converge", "label": "形末量尽遇到均线联合（黑马基因）",
     "expr": "MA_SPREAD([5, 10, 20, 60]) <= 0.03 AND RANGE_CONTRACT(10) < 1 AND VOL_CONTRACT(10) < 1",
     "look": "没给数字：四线离散 3% 以内、最近 10 根波幅和量都比前 10 根小，是研究取值"},
    {"id": "redgreen", "label": "红肥绿瘦（黑马基因）", "expr": "RED_GREEN_SHARE(20) >= 0.6",
     "look": "20 根里阳线的量占六成以上（研究取值）；只描述量柱，不推断资金"},
    {"id": "strong", "label": "强于大盘（黑马基因）", "expr": "REL_PERF(20) > 0",
     "look": "20 根里比对照指数强；看强弱本身有没有超额"},
    {"id": "macd", "label": "MACD 水上金叉（看 MACD 在零轴上方还是下方）", "expr": "CROSS(MACD.DIF, MACD.DEA) AND MACD.DIF > 0",
     "look": "零轴上方的金叉当事件，和随机入场比"},
]
