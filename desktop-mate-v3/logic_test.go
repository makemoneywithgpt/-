package main

import (
	"testing"
	"time"
)

func tdate(day int, hh, mm, ss int) time.Time {
	return time.Date(2026, 11, day, hh, mm, ss, 0, time.Local)
}
func TestStatus(t *testing.T) {
	s := defaultSettings()
	tests := []struct {
		day, h, m, sec int
		want           string
		remain         string
	}{
		{23, 8, 0, 0, "before", "01:00:00"},
		{23, 9, 0, 0, "work", "09:00:00"},
		{23, 11, 40, 0, "lunch_soon", "00:20:00"},
		{23, 15, 10, 0, "afternoon", "02:50:00"},
		{23, 12, 0, 0, "lunch", "01:00:00"},
		{23, 17, 58, 10, "soon", "00:01:50"},
		{23, 18, 0, 0, "done", ""},
		{22, 12, 0, 0, "weekend", ""},
	}
	for _, tt := range tests {
		r := statusAt(tdate(tt.day, tt.h, tt.m, tt.sec), s)
		if r.Label != tt.want {
			t.Errorf("got %s want %s", r.Label, tt.want)
		}
		if r.Timed && countdownFormat(r.Remaining) != tt.remain {
			t.Errorf("remaining %s want %s", countdownFormat(r.Remaining), tt.remain)
		}
	}
}
func TestSettings(t *testing.T) {
	s := Settings{Start: "bad", End: "20:99", Scale: 3}
	v := validSettings(s)
	if v.Start != "09:00" || v.End != "18:00" || v.Scale != 100 {
		t.Fatal(v)
	}
}
func TestClock(t *testing.T) {
	if countdownFormat(78*time.Minute+15*time.Second) != "01:18:15" {
		t.Fatal("bad countdown")
	}
}
