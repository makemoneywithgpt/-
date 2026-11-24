package main

import (
	"bytes"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"os"
	"strings"
	"testing"
	"testing/fstest"
	"time"
)

func fixturePNG(t *testing.T, x int) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, 8, 8))
	img.SetNRGBA(x, 4, color.NRGBA{R: 255, A: 255})
	var data bytes.Buffer
	if err := png.Encode(&data, img); err != nil {
		t.Fatal(err)
	}
	return data.Bytes()
}

func TestPackTiming(t *testing.T) {
	source := fstest.MapFS{
		"pack.json": {Data: []byte(`{"canvas":[8,8],"clips":{"idle_basic":[{"file":"first.png","ms":580},{"file":"next.png","ms":70}]}}`)},
		"first.png": {Data: fixturePNG(t, 2)},
		"next.png":  {Data: fixturePNG(t, 4)},
	}
	pack, err := readPack(source)
	if err != nil {
		t.Fatal(err)
	}
	idle := pack[Idle]
	for _, tc := range []struct{ elapsed, x, wait int }{{0, 2, 580}, {579, 2, 1}, {580, 4, 70}, {650, 2, 580}} {
		frame, next := idle.frameAt(time.Duration(tc.elapsed) * time.Millisecond)
		_, _, _, alpha := frame.At(tc.x, 4).RGBA()
		if alpha == 0 || next.Milliseconds() != int64(tc.wait) {
			t.Errorf("at %dms: pixel alpha %d, next after %v", tc.elapsed, alpha, next)
		}
	}
	source["next.png"].Data = fixturePNG(t, 0)
	if _, err := readPack(source); err == nil || !strings.Contains(err.Error(), "padding") {
		t.Fatalf("expected transparent-padding error, got %v", err)
	}
	delete(source, "next.png")
	if _, err := readPack(source); err == nil || !strings.Contains(err.Error(), "next.png") {
		t.Fatalf("expected missing frame error, got %v", err)
	}
}

func TestDeliveredPackMatchesSourceTiming(t *testing.T) {
	pack, err := readPack(os.DirFS("assets/actions"))
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile("review/source-timing.json")
	if err != nil {
		t.Fatal(err)
	}
	var reports map[Behavior]struct {
		FrameMS []int `json:"frame_ms"`
		TotalMS int   `json:"total_ms"`
	}
	if err := json.Unmarshal(data, &reports); err != nil {
		t.Fatal(err)
	}
	work := pack[Work]
	want := reports[Work]
	if len(work.images) != len(want.FrameMS) || len(work.images) != 50 ||
		work.duration().Milliseconds() != int64(want.TotalMS) {
		t.Fatal("delivered pack timing differs from original")
	}
	var elapsed time.Duration
	for i, ms := range want.FrameMS {
		elapsed += time.Duration(ms) * time.Millisecond
		if work.ends[i] != elapsed {
			t.Fatalf("frame %d timing differs", i+1)
		}
	}
	if len(pack[Idle].images) != 1 || len(pack) != 2 {
		t.Fatal("unexpected shipped clips")
	}
}
