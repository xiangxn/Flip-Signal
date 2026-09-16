package config

import (
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/spf13/viper"
)

// configFilePath 是仓库根目录的默认值示例文件（go test 的 CWD = 本包目录）。
const configFilePath = "../../v4.config.yaml"

// TestConfigFileMatchesDefaults 漂移守卫: v4.config.yaml 必须逐字段等于代码默认值。
//
// 该文件身兼两职（默认值文档 + 可直接跑的配置），所以"某人为了自己的部署把值改掉"
// 是这里最需要拦下来的失败——文件头已写明要改请复制成 config.local.yaml。
func TestConfigFileMatchesDefaults(t *testing.T) {
	got, err := Load(configFilePath)
	if err != nil {
		t.Fatalf("加载 %s 失败: %v", configFilePath, err)
	}
	want := defaults()
	if reflect.DeepEqual(got, want) {
		return
	}

	// 逐叶对比: 整结构体 dump 没法看出到底哪个键漂了
	gotLeaves := flatten(got)
	wantLeaves := flatten(want)
	keys := map[string]bool{}
	for k := range gotLeaves {
		keys[k] = true
	}
	for k := range wantLeaves {
		keys[k] = true
	}
	sorted := make([]string, 0, len(keys))
	for k := range keys {
		sorted = append(sorted, k)
	}
	sort.Strings(sorted)

	for _, k := range sorted {
		g, gok := gotLeaves[k]
		w, wok := wantLeaves[k]
		switch {
		case !gok:
			t.Errorf("%s: 文件缺失（默认值 %v）—— 新增配置项后忘了写进 v4.config.yaml", k, w)
		case !wok:
			t.Errorf("%s: 文件多出（文件值 %v）—— 默认值里没有这个键", k, g)
		case !reflect.DeepEqual(g, w):
			t.Errorf("%s: 文件值 %v ≠ 默认值 %v", k, g, w)
		}
	}
}

// TestConfigFileCoversAllDefaults 覆盖度: defaults() 的每个叶子键都必须出现在文件里。
//
// 与上一个测试互补——DeepEqual 只能发现"文件里写了但值不对"，
// 发现不了"新增字段、文件里压根没这个键"（缺键时预置默认值原样保留，DeepEqual 仍通过）。
func TestConfigFileCoversAllDefaults(t *testing.T) {
	v := viper.New()
	v.SetConfigFile(configFilePath)
	if err := v.ReadInConfig(); err != nil {
		t.Fatalf("读取 %s 失败: %v", configFilePath, err)
	}

	inFile := map[string]bool{}
	for _, k := range v.AllKeys() {
		inFile[strings.ToLower(k)] = true
	}

	for _, k := range leafKeys(defaults()) {
		if !inFile[k] {
			t.Errorf("%s: v4.config.yaml 里没有这个键（新增配置项忘了写示例文件？）", k)
		}
	}
}

// flatten 把配置结构体摊平成"叶子键 → 值"，nil 指针子树跳过。
func flatten(v any) map[string]any {
	out := map[string]any{}
	for _, k := range leafKeys(v) {
		out[k] = leafValue(reflect.ValueOf(v), strings.Split(k, "."))
	}
	return out
}

// leafValue 沿路径取值（仅供本测试的报错打印用）。
func leafValue(rv reflect.Value, path []string) any {
	for _, name := range path {
		rv = deref(rv)
		if !rv.IsValid() || rv.Kind() != reflect.Struct {
			return nil
		}
		rt := rv.Type()
		found := false
		for i := 0; i < rt.NumField(); i++ {
			if fieldKey(rt.Field(i)) == name {
				rv = rv.Field(i)
				found = true
				break
			}
		}
		if !found {
			return nil
		}
	}
	rv = deref(rv)
	if !rv.IsValid() {
		return nil
	}
	return rv.Interface()
}

// leafKeys 汇总结构体的全部叶子键路径（点号分隔，取 mapstructure tag），
// nil 指针/接口子树跳过——与 config.Load 的解码行为一致（缺键保留默认值，
// 空指针不会凭空变成空凭证对象）。
func leafKeys(v any) []string {
	var out []string
	collectLeaves(reflect.ValueOf(v), "", &out)
	sort.Strings(out)
	return out
}

func collectLeaves(rv reflect.Value, prefix string, out *[]string) {
	rv = deref(rv)
	if !rv.IsValid() {
		// nil 指针/接口: 整棵子树跳过（解码时它也确实不会被赋值）
		return
	}
	if rv.Kind() != reflect.Struct {
		if prefix != "" {
			*out = append(*out, prefix)
		}
		return
	}
	rt := rv.Type()
	for i := 0; i < rt.NumField(); i++ {
		f := rt.Field(i)
		if !f.IsExported() {
			continue
		}
		name := fieldKey(f)
		if name == "" || name == "-" {
			continue
		}
		key := name
		if prefix != "" {
			key = prefix + "." + name
		}
		collectLeaves(rv.Field(i), key, out)
	}
}

// deref 剥掉指针/接口（nil 返回零值，调用方按 Kind 处理）。
func deref(rv reflect.Value) reflect.Value {
	for rv.Kind() == reflect.Ptr || rv.Kind() == reflect.Interface {
		if rv.IsNil() {
			return reflect.Value{}
		}
		rv = rv.Elem()
	}
	return rv
}

// fieldKey 返回字段的 mapstructure tag 名（无 tag 回退 Go 字段名）。
func fieldKey(f reflect.StructField) string {
	if tag := f.Tag.Get("mapstructure"); tag != "" {
		return strings.Split(tag, ",")[0]
	}
	return f.Name
}
