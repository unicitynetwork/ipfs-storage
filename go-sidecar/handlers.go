package main

import (
	"database/sql"
	"encoding/base64"
	"fmt"
	"io"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"sync"
	"time"
)

var argRE = regexp.MustCompile(`^/ipns/(.+)$`)

// Handler serves HTTP requests for the Go sidecar.
type Handler struct {
	db         *sql.DB
	cfg        Config
	kuboClient *http.Client
	store      *Store

	// Track in-flight background refreshes to prevent duplicates.
	mu              sync.Mutex
	refreshInFlight map[string]struct{}
}

// RoutingGet handles GET/POST /routing-get?arg=/ipns/{name}.
//
// Flow:
//  1. Query SQLite for cached response_cache (5-20ms)
//  2. If found, return immediately; trigger async refresh if stale
//  3. If not found, blocking DHT fetch via kubo, store, return
func (h *Handler) RoutingGet(w http.ResponseWriter, r *http.Request) {
	arg := r.URL.Query().Get("arg")
	m := argRE.FindStringSubmatch(arg)
	if m == nil {
		http.Error(w, "Invalid arg format", http.StatusBadRequest)
		return
	}
	ipnsName := m[1]

	if !isValidIPNSName(ipnsName) {
		http.Error(w, "Invalid IPNS name", http.StatusBadRequest)
		return
	}

	// 1. Query SQLite.
	var responseCache sql.NullString
	var marshalledRecord []byte
	var sequence int64
	var lastUpdated sql.NullString
	err := h.db.QueryRow(
		`SELECT response_cache, marshalled_record, COALESCE(sequence, 0), last_updated
		 FROM ipns_records WHERE ipns_name = ?`, ipnsName,
	).Scan(&responseCache, &marshalledRecord, &sequence, &lastUpdated)

	if err == sql.ErrNoRows {
		// Not in cache — blocking DHT fetch.
		h.fetchFromDHTAndStore(w, ipnsName)
		return
	}
	if err != nil {
		log.Printf("routing-get: DB error for %s: %v", truncate(ipnsName), err)
		http.Error(w, "Internal error", http.StatusInternalServerError)
		return
	}

	// Compute response body.
	var body string
	if responseCache.Valid && responseCache.String != "" {
		body = responseCache.String
	} else {
		body = buildResponseJSON(marshalledRecord)
	}

	// Calculate age and staleness.
	ageSeconds := 0
	isStale := true
	if lastUpdated.Valid && lastUpdated.String != "" {
		if t, err := parseTimestamp(lastUpdated.String); err == nil {
			ageSeconds = int(time.Since(t).Seconds())
			isStale = ageSeconds > h.cfg.StaleThresholdSeconds
		}
	}

	// If stale, trigger non-blocking background refresh.
	if isStale {
		h.triggerAsyncRefresh(ipnsName, sequence)
	}

	// Return cached response.
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-IPNS-Source", "go-sidecar")
	w.Header().Set("X-IPNS-Sequence", strconv.FormatInt(sequence, 10))
	if lastUpdated.Valid {
		w.Header().Set("X-IPNS-Last-Updated", lastUpdated.String)
	}
	w.Header().Set("X-IPNS-Age", strconv.Itoa(ageSeconds))
	w.Header().Set("X-IPNS-Stale", strconv.FormatBool(isStale))
	w.Header().Set("Cache-Control", fmt.Sprintf("max-age=%d, stale-while-revalidate=30", h.cfg.StaleThresholdSeconds))
	w.WriteHeader(http.StatusOK)
	io.WriteString(w, body)
}

// fetchFromDHTAndStore does a blocking DHT fetch for records not in SQLite.
func (h *Handler) fetchFromDHTAndStore(w http.ResponseWriter, ipnsName string) {
	recordBytes, seq, err := fetchFromKubo(h.kuboClient, h.cfg.KuboAPIURL, ipnsName)
	if err != nil {
		log.Printf("routing-get: DHT fetch error for %s: %v", truncate(ipnsName), err)
		http.Error(w, "Not found", http.StatusNotFound)
		return
	}

	responseJSON := buildResponseJSON(recordBytes)
	_, cid := parseIPNSRecord(recordBytes)
	h.store.WriteRecord(ipnsName, recordBytes, responseJSON, seq, cid)

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-IPNS-Source", "kubo")
	w.Header().Set("X-IPNS-Sequence", strconv.FormatInt(seq, 10))
	w.WriteHeader(http.StatusOK)
	io.WriteString(w, responseJSON)
}

// triggerAsyncRefresh starts a background goroutine to refresh a stale record.
func (h *Handler) triggerAsyncRefresh(ipnsName string, dbSequence int64) {
	h.mu.Lock()
	if _, ok := h.refreshInFlight[ipnsName]; ok {
		h.mu.Unlock()
		return
	}
	if len(h.refreshInFlight) >= h.cfg.MaxRefreshConcurrency {
		h.mu.Unlock()
		return
	}
	h.refreshInFlight[ipnsName] = struct{}{}
	h.mu.Unlock()

	go func() {
		defer func() {
			h.mu.Lock()
			delete(h.refreshInFlight, ipnsName)
			h.mu.Unlock()
		}()

		recordBytes, seq, err := fetchFromKubo(h.kuboClient, h.cfg.KuboAPIURL, ipnsName)
		if err != nil {
			return
		}

		if seq > dbSequence {
			responseJSON := buildResponseJSON(recordBytes)
			_, cid := parseIPNSRecord(recordBytes)
			h.store.WriteRecord(ipnsName, recordBytes, responseJSON, seq, cid)
			h.store.NotifyPython(ipnsName, seq, cid)
		} else {
			h.store.TouchTimestamp(ipnsName)
		}
	}()
}

// Health returns a simple health check response.
func (h *Handler) Health(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain")
	io.WriteString(w, "ok\n")
}

// fetchFromKubo fetches an IPNS record from the kubo DHT API.
func fetchFromKubo(client *http.Client, kuboURL, ipnsName string) ([]byte, int64, error) {
	url := fmt.Sprintf("%s/api/v0/routing/get?arg=/ipns/%s", kuboURL, ipnsName)
	req, err := http.NewRequest("POST", url, nil)
	if err != nil {
		return nil, 0, err
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, resp.Body)
		return nil, 0, fmt.Errorf("kubo returned %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, 0, err
	}

	extraB64 := extractExtraField(body)
	if extraB64 == "" {
		return nil, 0, fmt.Errorf("no Extra field in kubo response")
	}

	recordBytes, err := base64.StdEncoding.DecodeString(extraB64)
	if err != nil {
		return nil, 0, fmt.Errorf("base64 decode: %w", err)
	}

	seq, _ := parseIPNSRecord(recordBytes)
	return recordBytes, seq, nil
}

// parseTimestamp handles SQLite timestamp formats.
func parseTimestamp(s string) (time.Time, error) {
	for _, layout := range []string{
		"2006-01-02 15:04:05",
		"2006-01-02T15:04:05",
		"2006-01-02T15:04:05Z",
		"2006-01-02T15:04:05+00:00",
		"2006-01-02T15:04:05.000000",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("unparseable timestamp: %s", s)
}

func truncate(s string) string {
	if len(s) > 16 {
		return s[:16] + "..."
	}
	return s
}
