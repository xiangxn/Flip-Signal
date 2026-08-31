package collect

import (
	"github.com/tidwall/gjson"
)

// ParseMarketTokens 从 gamma 市场 JSON 解析 UP/DOWN token ID。
//
// outcomes 约定 [0]=Up [1]=Down（与 lab 一致），clobTokenIds 与之对齐；
// 支持 "Up"/"Yes"、"Down"/"No" 两种 outcome 命名。未找到时返回空串。
func ParseMarketTokens(data *gjson.Result) (upTokenID, downTokenID string) {
	clobRaw := data.Get("clobTokenIds").String()
	var tokenIDs []string
	for _, v := range gjson.Parse(clobRaw).Array() {
		tokenIDs = append(tokenIDs, v.String())
	}

	outcomesRaw := data.Get("outcomes").String()
	var outcomes []string
	for _, v := range gjson.Parse(outcomesRaw).Array() {
		outcomes = append(outcomes, v.String())
	}

	for i, oc := range outcomes {
		if i >= len(tokenIDs) {
			break
		}
		switch oc {
		case "Up", "Yes":
			upTokenID = tokenIDs[i]
		case "Down", "No":
			downTokenID = tokenIDs[i]
		}
	}
	return upTokenID, downTokenID
}
