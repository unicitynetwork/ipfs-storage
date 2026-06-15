package main

import (
	"database/sql"
	"log"
)

// Store encapsulates shared SQLite write operations and WebSocket notifications.
type Store struct {
	db     *sql.DB
	subMgr *SubscriptionManager
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

// NotifyWS pushes an IPNS update to all WebSocket subscribers.
// Called after DHT refresh finds a newer record.
func (s *Store) NotifyWS(ipnsName string, sequence int64, cid string) {
	if s.subMgr != nil {
		s.subMgr.Notify(ipnsName, sequence, cid)
	}
}
