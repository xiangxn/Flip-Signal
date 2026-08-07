package dashboard

import (
	"embed"
	"io/fs"
	"net/http"
)

//go:embed static/*
var staticFS embed.FS

// static is the sub-filesystem rooted at static/.
var static http.FileSystem

func init() {
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		panic("dashboard: embedded static/ not found: " + err.Error())
	}
	static = http.FS(sub)
}

func (s *State) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	index, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		http.Error(w, "index not found", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	w.Write(index)
}
