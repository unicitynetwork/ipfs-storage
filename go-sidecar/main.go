// Go sidecar for IPNS routing-get and DHT refresh.
//
// Eliminates CPython pymalloc fragmentation (issue #17) by moving the
// high-throughput read path and DHT refresh out of the Python pinner
// into a compiled binary with predictable memory behaviour.
//
// Shares the SQLite database with the Python pinner via WAL mode.
// Python handles writes (ipns-intercept, chain validation, Nostr).
// Go handles reads (/routing-get) and background DHT refresh.
package main

import (
	"context"
	"database/sql"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// Config holds runtime configuration from environment variables.
type Config struct {
	ListenAddr            string
	DBPath                string
	KuboAPIURL            string
	StaleThresholdSeconds int
	RefreshInterval       time.Duration
	RefreshBatchSize      int
	MaxRefreshConcurrency int
	PythonSidecarURL      string
}

func loadConfig() Config {
	c := Config{
		ListenAddr:            ":" + envOr("GO_SIDECAR_PORT", "9082"),
		DBPath:                envOr("DB_PATH", "/data/ipfs/propagation.db"),
		KuboAPIURL:            envOr("IPFS_API_URL", "http://127.0.0.1:5001"),
		StaleThresholdSeconds: envOrInt("STALE_THRESHOLD_SECONDS", 60),
		RefreshBatchSize:      envOrInt("REFRESH_BATCH_SIZE", 50),
		MaxRefreshConcurrency: envOrInt("MAX_REFRESH_CONCURRENCY", 10),
		PythonSidecarURL:      envOr("PYTHON_SIDECAR_URL", "http://127.0.0.1:9081"),
	}
	c.RefreshInterval = time.Duration(envOrInt("REFRESH_INTERVAL_SECONDS", 10)) * time.Second
	return c
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envOrInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func main() {
	cfg := loadConfig()

	// Open SQLite in WAL mode, read-write (needed for DHT refresh writes).
	db, err := sql.Open("sqlite3", cfg.DBPath+"?_journal_mode=WAL&_busy_timeout=5000&cache=shared")
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}
	defer db.Close()

	if err := db.Ping(); err != nil {
		log.Fatalf("Database ping failed: %v", err)
	}
	log.Printf("Connected to SQLite: %s", cfg.DBPath)

	// HTTP client for kubo API calls.
	kuboClient := &http.Client{
		Timeout: 15 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns:        20,
			MaxIdleConnsPerHost: 20,
			IdleConnTimeout:     90 * time.Second,
		},
	}

	// Notification client for Python sidecar WS callbacks (short timeout).
	notifyClient := &http.Client{Timeout: 2 * time.Second}

	store := &Store{
		db:           db,
		notifyClient: notifyClient,
		pythonURL:    cfg.PythonSidecarURL,
	}

	handler := &Handler{
		db:              db,
		cfg:             cfg,
		kuboClient:      kuboClient,
		store:           store,
		refreshInFlight: make(map[string]struct{}),
	}

	refresher := &Refresher{
		db:         db,
		cfg:        cfg,
		kuboClient: kuboClient,
		store:      store,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/routing-get", handler.RoutingGet)
	mux.HandleFunc("/health", handler.Health)

	srv := &http.Server{
		Addr:         cfg.ListenAddr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go refresher.Run(ctx)

	go func() {
		log.Printf("Go sidecar listening on %s", cfg.ListenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Server error: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("Shutting down...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("Shutdown error: %v", err)
	}
	log.Println("Go sidecar stopped")
}
