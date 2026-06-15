package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"
)

// Refresher periodically scans for stale IPNS records and refreshes them
// from the kubo DHT. This replaces the Python _refresh_and_push and
// DhtSyncWorker, eliminating CPython pymalloc fragmentation from
// high-throughput HTTP + base64 allocations.
type Refresher struct {
	db         *sql.DB
	cfg        Config
	kuboClient *http.Client
	store      *Store
}

// Run starts the periodic refresh loop. Blocks until ctx is cancelled.
func (r *Refresher) Run(ctx context.Context) {
	log.Printf("DHT refresh worker started (interval=%s, stale=%ds, batch=%d, concurrency=%d)",
		r.cfg.RefreshInterval, r.cfg.StaleThresholdSeconds,
		r.cfg.RefreshBatchSize, r.cfg.MaxRefreshConcurrency)

	ticker := time.NewTicker(r.cfg.RefreshInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Println("DHT refresh worker stopped")
			return
		case <-ticker.C:
			r.refreshBatch(ctx)
		}
	}
}

// refreshBatch finds stale records and refreshes them concurrently.
func (r *Refresher) refreshBatch(ctx context.Context) {
	staleThreshold := fmt.Sprintf("-%d seconds", r.cfg.StaleThresholdSeconds)
	rows, err := r.db.QueryContext(ctx,
		`SELECT ipns_name, COALESCE(sequence, 0) as sequence
		 FROM ipns_records
		 WHERE last_updated < datetime('now', ?)
		 ORDER BY last_updated ASC
		 LIMIT ?`,
		staleThreshold, r.cfg.RefreshBatchSize,
	)
	if err != nil {
		log.Printf("refresh: query error: %v", err)
		return
	}
	defer rows.Close()

	type staleRecord struct {
		name     string
		sequence int64
	}
	var stale []staleRecord
	for rows.Next() {
		var sr staleRecord
		if err := rows.Scan(&sr.name, &sr.sequence); err != nil {
			continue
		}
		stale = append(stale, sr)
	}

	if len(stale) == 0 {
		return
	}

	log.Printf("refresh: processing %d stale records", len(stale))

	sem := make(chan struct{}, r.cfg.MaxRefreshConcurrency)
	var wg sync.WaitGroup

	for _, sr := range stale {
		if ctx.Err() != nil {
			break
		}

		sem <- struct{}{}
		wg.Add(1)
		go func(name string, seq int64) {
			defer func() {
				<-sem
				wg.Done()
			}()
			r.refreshSingle(name, seq)
		}(sr.name, sr.sequence)
	}

	wg.Wait()
}

// refreshSingle refreshes a single IPNS record from the kubo DHT.
func (r *Refresher) refreshSingle(ipnsName string, dbSequence int64) {
	recordBytes, seq, err := fetchFromKubo(r.kuboClient, r.cfg.KuboAPIURL, ipnsName)
	if err != nil {
		// Touch timestamp to avoid retrying immediately.
		r.store.TouchTimestamp(ipnsName)
		return
	}

	if seq > dbSequence {
		responseJSON := buildResponseJSON(recordBytes)
		_, cid := parseIPNSRecord(recordBytes)
		r.store.WriteRecord(ipnsName, recordBytes, responseJSON, seq, cid)
		r.store.NotifyPython(ipnsName, seq, cid)
		log.Printf("refresh: updated %s seq %d→%d", truncate(ipnsName), dbSequence, seq)
	} else {
		r.store.TouchTimestamp(ipnsName)
	}
}

