package main

import (
	"encoding/json"
	"fmt"
	"image"
	"image/png"
	"io/fs"
	"path"
	"sort"
	"strings"
	"time"
)

type frameSpec struct {
	File string `json:"file"`
	MS   int    `json:"ms"`
}

type clip struct {
	images []image.Image
	ends   []time.Duration
}

func (c clip) duration() time.Duration { return c.ends[len(c.ends)-1] }

// frameAt uses original cumulative durations so scheduling delays do not accumulate.
func (c clip) frameAt(elapsed time.Duration) (image.Image, time.Duration) {
	elapsed %= c.duration()
	index := sort.Search(len(c.ends), func(i int) bool { return c.ends[i] > elapsed })
	return c.images[index], c.ends[index] - elapsed
}

func readPack(source fs.FS) (map[Behavior]clip, error) {
	raw, err := fs.ReadFile(source, "pack.json")
	if err != nil {
		return nil, err
	}
	var spec struct {
		Canvas []int                    `json:"canvas"`
		Clips  map[Behavior][]frameSpec `json:"clips"`
	}
	if err := json.Unmarshal(raw, &spec); err != nil {
		return nil, err
	}
	if len(spec.Canvas) != 2 || spec.Canvas[0] < 3 || spec.Canvas[0] > 4096 || spec.Canvas[1] < 3 || spec.Canvas[1] > 4096 {
		return nil, fmt.Errorf("canvas must have two dimensions between 3 and 4096")
	}
	if len(spec.Clips[Idle]) == 0 {
		return nil, fmt.Errorf("idle clip is required")
	}
	canvas := image.Rect(0, 0, spec.Canvas[0], spec.Canvas[1])
	clips := make(map[Behavior]clip)
	images := make(map[string]image.Image)
	const pixelBudget = 32 * 1024 * 1024
	for kind, frames := range spec.Clips {
		if (kind != Idle && kind != Drink && kind != Doze) || len(frames) == 0 {
			return nil, fmt.Errorf("unsupported or empty clip %q", kind)
		}
		var result clip
		var elapsed time.Duration
		for _, frame := range frames {
			if frame.MS < 1 || frame.MS > 2_147_483_647 || !fs.ValidPath(frame.File) ||
				strings.Contains(frame.File, `\`) || strings.ToLower(path.Ext(frame.File)) != ".png" {
				return nil, fmt.Errorf("%s: invalid frame file or duration: %q", kind, frame.File)
			}
			sprite := images[frame.File]
			if sprite == nil {
				if len(images)+1 > pixelBudget/(canvas.Dx()*canvas.Dy()) {
					return nil, fmt.Errorf("animation pack exceeds decoded image budget; shorten clips or reduce the canvas")
				}
				sprite, err = readFrame(source, frame.File, canvas)
				if err != nil {
					return nil, fmt.Errorf("%s: %s: %w", kind, frame.File, err)
				}
				images[frame.File] = sprite
			}
			elapsed += time.Duration(frame.MS) * time.Millisecond
			result.images = append(result.images, sprite)
			result.ends = append(result.ends, elapsed)
		}
		clips[kind] = result
	}
	return clips, nil
}

func readFrame(source fs.FS, name string, canvas image.Rectangle) (image.Image, error) {
	file, err := source.Open(name)
	if err != nil {
		return nil, err
	}
	config, err := png.DecodeConfig(file)
	file.Close()
	if err != nil {
		return nil, err
	}
	if config.Width != canvas.Dx() || config.Height != canvas.Dy() {
		return nil, fmt.Errorf("expected %dx%d canvas", canvas.Dx(), canvas.Dy())
	}
	file, err = source.Open(name)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	sprite, err := png.Decode(file)
	if err != nil {
		return nil, err
	}
	if sprite.Bounds() != canvas {
		return nil, fmt.Errorf("canvas changed while reading frame")
	}
	visible := false
	for y := 0; y < canvas.Dy(); y++ {
		for x := 0; x < canvas.Dx(); x++ {
			_, _, _, alpha := sprite.At(x, y).RGBA()
			if alpha == 0 {
				continue
			}
			if x == 0 || y == 0 || x == canvas.Dx()-1 || y == canvas.Dy()-1 {
				return nil, fmt.Errorf("missing transparent padding")
			}
			visible = true
		}
	}
	if !visible {
		return nil, fmt.Errorf("empty frame")
	}
	return sprite, nil
}
