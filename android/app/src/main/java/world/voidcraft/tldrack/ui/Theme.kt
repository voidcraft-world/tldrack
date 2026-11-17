package world.voidcraft.tldrack.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Shapes
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import java.time.ZonedDateTime
import kotlinx.coroutines.delay

// The rack's green glass palette: near-black glass, mint ink, amber for what needs the operator.
// res/values/colors.xml repeats `void` for the window and the launcher icon, which draw before Compose does.
object RackColors {
    val void = Color(0xFF050B08)
    val glass = Color(0xFF08100C)
    val mint = Color(0xFF93FFD4)
    val accent = Color(0xFFBFFFE2)
    val text = Color(0xFFCCFFE7)
    val dim = Color(0x99CCFFE7)
    val line = Color(0x5293FFD4)
    val warn = Color(0xFFFFC46B)
}

val RackMono = FontFamily.Monospace
/** The house corner, for a surface that draws its own frame (LAYA's opened row) rather than taking `RackShape`. */
val RackCorner = 3.dp
val RackShape = RoundedCornerShape(RackCorner)

@Composable
fun TldrackTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = RackColors.mint,
            onPrimary = RackColors.void,
            background = RackColors.void,
            onBackground = RackColors.text,
            surface = RackColors.glass,
            onSurface = RackColors.text,
            surfaceVariant = RackColors.glass,
            onSurfaceVariant = RackColors.dim,
            outline = RackColors.line,
            error = RackColors.warn,
            onError = RackColors.void,
        ),
        shapes = Shapes(extraSmall = RackShape, small = RackShape, medium = RackShape, large = RackShape),
        content = content,
    )
}

/** Mono label for heads and short readouts (FUI rule 5) — never a sentence. Callers pass their own casing. */
@Composable
fun TechLabel(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = RackColors.accent,
    size: TextUnit = 12.sp,
) {
    Text(text, modifier = modifier, color = color, fontFamily = RackMono, fontSize = size, letterSpacing = 0.08.em)
}

@Composable
fun FuiButton(
    label: String,
    on_click: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    color: Color = RackColors.mint,
    content_padding: PaddingValues = ButtonDefaults.ContentPadding,
) {
    OutlinedButton(
        onClick = on_click,
        enabled = enabled,
        modifier = modifier,
        shape = RackShape,
        contentPadding = content_padding,
        border = BorderStroke(1.dp, if (enabled) color.copy(alpha = 0.6f) else RackColors.line),
        colors = ButtonDefaults.outlinedButtonColors(contentColor = color, disabledContentColor = RackColors.dim),
    ) {
        Text(label, fontFamily = RackMono, fontSize = 13.sp, letterSpacing = 0.08.em)
    }
}

/**
 * Wall-clock time that steps once per minute, on the minute. Readouts built on
 * it ("until 17:00") change as discrete state, not as a ticking animation.
 */
@Composable
fun rememberMinuteClock(): ZonedDateTime {
    var now by remember { mutableStateOf(ZonedDateTime.now()) }
    LaunchedEffect(Unit) {
        while (true) {
            val current = ZonedDateTime.now()
            now = current
            delay(60_000L - (current.second * 1_000L + current.nano / 1_000_000L) + 50L)
        }
    }
    return now
}
