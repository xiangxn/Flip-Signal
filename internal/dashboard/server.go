package dashboard

import (
	"log"
	"net/http"
	"time"
)

// ListenAndServe starts the HTTP dashboard on the given address.
// Does not return unless the server fails to start.
func (s *State) ListenAndServe(addr string) {
	mux := http.NewServeMux()

	// Static files (CSS, JS)
	mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(static)))

	// HTML page
	mux.HandleFunc("/", s.handleIndex)

	// JSON API
	mux.HandleFunc("/api/state", s.handleState)
	mux.HandleFunc("/api/signals", s.handleSignals)
	mux.HandleFunc("/api/snapshots", s.handleSnapshots)
	mux.HandleFunc("/api/histrange", s.handleHistRange)
	mux.HandleFunc("/api/config", s.handleConfig)

	server := &http.Server{
		Addr:         addr,
		Handler:      withLogging(mux),
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  30 * time.Second,
	}

	log.Printf("[Dashboard] listening on http://localhost%s", addr)
	if err := server.ListenAndServe(); err != nil {
		log.Printf("[Dashboard] server error: %v", err)
	}
}

// withLogging wraps a handler with basic request logging.
func withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		if r.URL.Path != "/api/state" { // don't spam for state polling
			log.Printf("[Dashboard] %s %s %v", r.Method, r.URL.Path, time.Since(start).Round(time.Microsecond))
		}
	})
}
