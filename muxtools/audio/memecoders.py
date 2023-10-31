"""
This contains a few either amusing or mostly useless codecs/encoders.

Thought they might be cool to have atleast.
"""

from pathlib import Path
from dataclasses import dataclass, field
from shlex import split as splitcommand
from typing import Sequence


from .encoders import FLAC
from .preprocess import Preprocessor, Resample
from .tools import Encoder, LosslessEncoder
from ..utils.log import crit, debug, error
from ..muxing.muxfiles import AudioFile
from ..utils.env import get_temp_workdir
from ..utils.download import get_executable
from ..utils.files import clean_temp_files, make_output
from ..utils.subprogress import run_cmd_pb, ProgressBarConfig
from .audioutils import ensure_valid_in, qaac_compatcheck, duration_from_track, get_preprocess_args
from ..utils.types import DitherType, LossyWavQuality, PathLike, ValidInputType

__all__ = ["qALAC", "TTA", "TrueAudio", "TheTrueAudio", "LossyWav", "Wavpack"]


@dataclass
class qALAC(Encoder):
    """
    Uses qAAC encoder to encode audio to ALAC.
    This is basically just worse FLAC and the only real use is good Apple hardware support.

    :param preprocess:          Any amount of preprocessors to run before passing it to the encoder.
    :param append:              Any other args one might pass to the encoder.
    :param output:              Custom output. Can be a dir or a file.
                                Do not specify an extension unless you know what you're doing.
    """

    preprocess: Preprocessor | Sequence[Preprocessor] | None = field(default_factory=Resample)
    append: str = ""
    output: PathLike | None = None

    def encode_audio(self, fileIn: AudioFile | PathLike, quiet: bool = True, **kwargs) -> AudioFile:
        if not isinstance(fileIn, AudioFile):
            fileIn = AudioFile.from_file(fileIn, self)
        output = make_output(fileIn.file, "alac", "qaac", self.output)
        source = ensure_valid_in(fileIn, preprocess=self.preprocess, caller=self, valid_type=ValidInputType.AIFF_OR_FLAC, supports_pipe=False)
        qaac = get_executable("qaac")
        qaac_compatcheck()

        debug(f"Encoding '{fileIn.file.stem}' to ALAC using qAAC...", self)
        args = [qaac, "-A", "--no-optimize", "--threading", "-o", str(output)]
        if self.append:
            args.extend(splitcommand(self.append))
        args.append(str(source.file))

        if not run_cmd_pb(args, quiet, ProgressBarConfig("Encoding...")):
            clean_temp_files()
            return AudioFile(output, fileIn.container_delay, fileIn.source)
        else:
            raise crit("Encoding to ALAC using qAAC failed!", self)


@dataclass
class TTA(LosslessEncoder):
    """
    Uses ffmpeg to encode audio to TTA/The True Audio.
    (I could not get the reference encoder to work with any ffmpeg wav or any flac)
    This doesn't really seem to have any benefit over FLAC except for maybe encode speed?
    Definitely has a cool name tho.


    :param preprocess:          Any amount of preprocessors to run before passing it to the encoder.
    :param append:              Any other args one might pass to the encoder
    :param output:              Custom output. Can be a dir or a file.
                                Do not specify an extension unless you know what you're doing.
    """

    preprocess: Preprocessor | Sequence[Preprocessor] | None = field(default_factory=Resample)
    append: str = ""
    output: PathLike | None = None

    def encode_audio(self, fileIn: AudioFile | PathLike, quiet: bool = True, **kwargs) -> AudioFile:
        if not isinstance(fileIn, AudioFile):
            fileIn = AudioFile.from_file(fileIn, self)
        output = make_output(fileIn.file, "tta", "encoded", self.output)

        args = [get_executable("ffmpeg"), "-hide_banner", "-i", str(fileIn.file.resolve()), "-map", "0:a:0", "-c:a", "tta"]
        args.extend(get_preprocess_args(fileIn, self.preprocess, fileIn.get_mediainfo(), self))
        if self.append:
            args.extend(splitcommand(self.append))
        args.append(str(output))

        debug(f"Encoding '{fileIn.file.stem}' to TTA using ffmpeg...", self)
        if not run_cmd_pb(args, quiet, ProgressBarConfig("Encoding...", duration_from_track(fileIn.get_mediainfo()))):
            clean_temp_files()
            return AudioFile(output, fileIn.container_delay, fileIn.source)
        else:
            raise crit("Encoding to TTA using ffmpeg failed!", self)


TrueAudio = TTA
TheTrueAudio = TTA


@dataclass
class Wavpack(LosslessEncoder):
    """
    Another interesting lossless codec even if solely for the fact that it supports 32bit float and an arbitrary amount of channels.
    Compression seems to be ever so slightly worse than FLAC from my very scarce testing.

    :param fast:                Use either fast or high quality modes. Obviously fast means less compression.
    :param preprocess:          Any amount of preprocessors to run before passing it to the encoder.
    :param append:              Any other args one might pass to the encoder
    :param output:              Custom output. Can be a dir or a file.
                                Do not specify an extension unless you know what you're doing.
    """

    fast: bool = False
    preprocess: Preprocessor | Sequence[Preprocessor] | None = field(default_factory=Resample)
    append: str = ""
    output: PathLike | None = None

    def encode_audio(self, fileIn: AudioFile | PathLike, quiet: bool = True, **kwargs) -> AudioFile:
        if not isinstance(fileIn, AudioFile):
            fileIn = AudioFile.from_file(fileIn, self)

        valid_in = ensure_valid_in(fileIn, False, self.preprocess, valid_type=ValidInputType.AIFF, caller=self)
        output = make_output(fileIn.file, "wv", "wavpack", self.output)

        wavpack = get_executable("wavpack")
        args = [wavpack, "-f" if self.fast else "-h"]
        if self.append:
            args.extend(splitcommand(self.append))
        args.extend([str(valid_in.file), str(output)])
        debug(f"Encoding '{fileIn.file.stem}' to wavpack...", self)
        if run_cmd_pb(args, quiet, ProgressBarConfig("Encoding...")):
            raise error("Failed to encode audio to wavpack!", self)

        clean_temp_files()
        return AudioFile(output, fileIn.container_delay, fileIn.source)


@dataclass
class LossyWav(Encoder):
    """
    A lossy (lol) preprocessor for wav/pcm audio that selectively reduces bitdepth by zero'ing out certain bits.
    Certain lossless encoders like FLAC (only the reference one) will get a massive size reduction that way.
    I don't really see a use for this over actual lossy codecs besides making a meme release.


    :param quality:             Lossywav Quality Preset
    :param target_encoder:      Whatever encoder the lossy wav file will be fed to. (lossless encoders only)
                                Only properly supports libFLAC and wavpack (will be added later) out of what we have.

    :param override_options:    Automatically sets the appropriate options for each encoder to work as intended.
    :param preprocess:          Any amount of preprocessors to run before passing it to the encoder.
    :param output:              Custom output. Can be a dir or a file.
                                Do not specify an extension unless you know what you're doing.
    """

    quality: LossyWavQuality = LossyWavQuality.INSANE
    target_encoder: LosslessEncoder | None = None
    override_options: bool = True
    preprocess: Preprocessor | Sequence[Preprocessor] | None = field(default_factory=Resample)
    output: PathLike | None = None

    def encode_audio(self, fileIn: AudioFile | PathLike, quiet: bool = True, **kwargs) -> AudioFile:
        if not self.target_encoder:
            self.target_encoder = FLAC()
        if not self.target_encoder.lossless:
            raise error("Target Encoder can only be a lossless one.", self)
        if not isinstance(fileIn, AudioFile):
            fileIn = AudioFile.from_file(fileIn, self)

        output = ensure_valid_in(fileIn, False, self.preprocess, valid_type=ValidInputType.W64, caller=self)

        args = [get_executable("lossyWAV", False), str(output.file), "--quality", self.quality.name.lower(), "-o", str(get_temp_workdir())]
        debug(f"Doing lossywav magic...", self)
        if run_cmd_pb(args, quiet, ProgressBarConfig("Encoding...")):
            raise crit("LossyWAV conversion failed!", self)

        lossy = Path(get_temp_workdir(), output.file.with_stem(output.file.stem + ".lossy").name)
        setattr(self.target_encoder, "preprocess", None)
        if self.override_options:
            if isinstance(self.target_encoder, FLAC):
                setattr(self.target_encoder, "compression_level", 5)
                setattr(self.target_encoder, "append", "-b 512")
            elif isinstance(self.target_encoder, Wavpack):
                setattr(self.target_encoder, "append", "--blocksize=512 --merge-blocks")

        encoded = self.target_encoder.encode_audio(AudioFile(lossy, fileIn.container_delay, fileIn.source), quiet)
        clean_temp_files()
        return encoded
