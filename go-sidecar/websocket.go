package main

import (
	"encoding/json"
	"log"
	"net/http"
	"regexp"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	// MaxSubscriptionsPerConn limits how many IPNS names a single client
	// can subscribe to, preventing memory exhaustion from malicious clients.
	MaxSubscriptionsPerConn = 200

	// MaxTotalConnections limits the total number of concurrent WebSocket
	// connections the sidecar will accept.
	MaxTotalConnections = 1000

	// wsReadLimit caps the maximum size of a single WebSocket message.
	wsReadLimit = 4096
)

var wsUpgrader = websocket.Upgrader{
	// Origin check intentionally permissive: this is a public IPNS
	// subscription service. CORS headers are added by nginx. Rate
	// limiting and subscription caps prevent abuse.
	CheckOrigin:     func(r *http.Request) bool { return true },
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
}

// safeConn wraps a websocket.Conn with a write mutex to prevent
// concurrent WriteMessage calls (gorilla/websocket is not safe for
// concurrent writes).
type safeConn struct {
	conn *websocket.Conn
	wmu  sync.Mutex
	// Number of subscriptions this connection holds.
	subCount int
}

func (sc *safeConn) writeJSON(data []byte) error {
	sc.wmu.Lock()
	defer sc.wmu.Unlock()
	sc.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	return sc.conn.WriteMessage(websocket.TextMessage, data)
}

// SubscriptionManager manages WebSocket subscriptions for IPNS updates.
type SubscriptionManager struct {
	mu   sync.RWMutex
	subs map[string]map[*safeConn]struct{} // ipnsName → set of conns

	connMu    sync.Mutex
	connCount int
}

func NewSubscriptionManager() *SubscriptionManager {
	return &SubscriptionManager{
		subs: make(map[string]map[*safeConn]struct{}),
	}
}

// wsMessage is the JSON envelope for client↔server WebSocket messages.
type wsMessage struct {
	Action    string   `json:"action,omitempty"`
	Type      string   `json:"type,omitempty"`
	Names     []string `json:"names,omitempty"`
	Name      string   `json:"name,omitempty"`
	Sequence  int64    `json:"sequence,omitempty"`
	CID       string   `json:"cid,omitempty"`
	Message   string   `json:"message,omitempty"`
	Timestamp string   `json:"timestamp,omitempty"`
}

var wsIPNSNameRE = regexp.MustCompile(`^(12D3KooW[a-zA-Z0-9]{44}|k[a-z2-7]{50,})$`)

// HandleWebSocket upgrades an HTTP connection and manages subscriptions.
func (sm *SubscriptionManager) HandleWebSocket(w http.ResponseWriter, r *http.Request) {
	// Enforce global connection limit.
	sm.connMu.Lock()
	if sm.connCount >= MaxTotalConnections {
		sm.connMu.Unlock()
		http.Error(w, "Too many connections", http.StatusServiceUnavailable)
		return
	}
	sm.connCount++
	sm.connMu.Unlock()

	conn, err := wsUpgrader.Upgrade(w, r, nil)
	if err != nil {
		sm.connMu.Lock()
		sm.connCount--
		sm.connMu.Unlock()
		return
	}

	sc := &safeConn{conn: conn}
	conn.SetReadLimit(wsReadLimit)

	defer func() {
		sm.removeAll(sc)
		conn.Close()
		sm.connMu.Lock()
		sm.connCount--
		sm.connMu.Unlock()
	}()

	conn.SetReadDeadline(time.Now().Add(90 * time.Second))
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(90 * time.Second))
		return nil
	})

	for {
		_, msgBytes, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err,
				websocket.CloseGoingAway,
				websocket.CloseNormalClosure,
				websocket.CloseNoStatusReceived) {
				log.Printf("ws: read error: %v", err)
			}
			break
		}

		conn.SetReadDeadline(time.Now().Add(90 * time.Second))

		var msg wsMessage
		if err := json.Unmarshal(msgBytes, &msg); err != nil {
			sm.sendJSON(sc, wsMessage{Type: "error", Message: "Invalid JSON"})
			continue
		}

		switch msg.Action {
		case "subscribe":
			var valid []string
			for _, name := range msg.Names {
				if sc.subCount >= MaxSubscriptionsPerConn {
					break
				}
				if wsIPNSNameRE.MatchString(name) {
					sm.addSub(name, sc)
					valid = append(valid, name)
				}
			}
			sm.sendJSON(sc, wsMessage{Type: "subscribed", Names: valid})

		case "unsubscribe":
			for _, name := range msg.Names {
				sm.removeSub(name, sc)
			}
			sm.sendJSON(sc, wsMessage{Type: "unsubscribed", Names: msg.Names})

		case "ping":
			sm.sendJSON(sc, wsMessage{Type: "pong"})

		default:
			sm.sendJSON(sc, wsMessage{Type: "error", Message: "Unknown action"})
		}
	}
}

// Notify pushes an IPNS update to all subscribers of the given name.
func (sm *SubscriptionManager) Notify(ipnsName string, sequence int64, cid string) {
	sm.mu.RLock()
	conns, ok := sm.subs[ipnsName]
	if !ok || len(conns) == 0 {
		sm.mu.RUnlock()
		return
	}
	targets := make([]*safeConn, 0, len(conns))
	for c := range conns {
		targets = append(targets, c)
	}
	sm.mu.RUnlock()

	msg := wsMessage{
		Type:      "update",
		Name:      ipnsName,
		Sequence:  sequence,
		CID:       cid,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}

	for _, sc := range targets {
		if err := sc.writeJSON(data); err != nil {
			sm.removeAll(sc)
			sc.conn.Close()
		}
	}
}

func (sm *SubscriptionManager) addSub(ipnsName string, sc *safeConn) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	if sm.subs[ipnsName] == nil {
		sm.subs[ipnsName] = make(map[*safeConn]struct{})
	}
	if _, exists := sm.subs[ipnsName][sc]; !exists {
		sm.subs[ipnsName][sc] = struct{}{}
		sc.subCount++
	}
}

func (sm *SubscriptionManager) removeSub(ipnsName string, sc *safeConn) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	if conns, ok := sm.subs[ipnsName]; ok {
		if _, exists := conns[sc]; exists {
			delete(conns, sc)
			sc.subCount--
			if len(conns) == 0 {
				delete(sm.subs, ipnsName)
			}
		}
	}
}

func (sm *SubscriptionManager) removeAll(sc *safeConn) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	for name, conns := range sm.subs {
		if _, exists := conns[sc]; exists {
			delete(conns, sc)
			sc.subCount--
			if len(conns) == 0 {
				delete(sm.subs, name)
			}
		}
	}
}

func (sm *SubscriptionManager) sendJSON(sc *safeConn, msg wsMessage) {
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}
	sc.writeJSON(data)
}
