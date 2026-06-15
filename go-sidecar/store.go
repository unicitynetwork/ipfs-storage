package main

import (
	"database/sql"
	"fmt"
	"log"
	"net/http"
	"net/url"
)

// Store encapsulates shared SQLite write operations and Python notification.
type Store struct {
	db           *sql.DB
	notifyClient *http.Client
	pythonURL    string
}

// WriteRecord inserts or updates an IPNS record in SQLite.
// Uses a simple sequence check — no chain validation (that's Python's concern
// on the ipns-intercept write path).
func (s *Store) WriteRecord(ipnsName string, marshalledRecord []byte, responseJSON string, sequence int64, cid string) {
	// Try UPDATE first (most common case for refresh).
	res, err := s.db.Exec(
		`UPDATE ipns_records
		 SET marshalled_record = ?, response_cache = ?, sequence = ?, cid = ?,
		     last_updated = datetime('now')
		 WHERE ipns_name = ? AND COALESCE(sequence, 0) < ?`,
		marshalledRecord, responseJSON, sequence, cid, ipnsName, sequence,
	)
	if err != nil {
		log.Printf("storeRecord UPDATE error: %v", err)
		return
	}

	rows, _ := res.RowsAffected()
	if rows > 0 {
		return
	}

	// If no rows updated, try INSERT (new record).
	_, err = s.db.Exec(
		`INSERT OR IGNORE INTO ipns_records
		 (ipns_name, marshalled_record, response_cache, sequence, cid, last_updated)
		 VALUES (?, ?, ?, ?, ?, datetime('now'))`,
		ipnsName, marshalledRecord, responseJSON, sequence, cid,
	)
	if err != nil {
		log.Printf("storeRecord INSERT error: %v", err)
	}
}

// TouchTimestamp updates last_updated without changing record data.
func (s *Store) TouchTimestamp(ipnsName string) {
	s.db.Exec(
		`UPDATE ipns_records SET last_updated = datetime('now') WHERE ipns_name = ?`,
		ipnsName,
	)
}

// NotifyPython sends a POST to the Python sidecar's /internal/ws-notify
// endpoint so it can push updates to connected WebSocket subscribers.
// Failures are non-critical — WS clients will get updates on next poll.
func (s *Store) NotifyPython(ipnsName string, sequence int64, cid string) {
	notifyURL := fmt.Sprintf("%s/internal/ws-notify?name=%s&sequence=%d&cid=%s",
		s.pythonURL, url.QueryEscape(ipnsName), sequence, url.QueryEscape(cid))
	resp, err := s.notifyClient.Post(notifyURL, "", nil)
	if err != nil {
		return
	}
	resp.Body.Close()
}
