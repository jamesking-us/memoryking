/** A bounded depth tilt of the bundled emblem; no continuous rendering or network requests. */
export function installEmblem(button) {
    const img = button.querySelector("img");
    let visible = true, animation = null;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    const stop = () => {
        animation?.cancel();
        animation = null;
        button.dataset.animating = "false";
    };
    stop();
    button.onclick = () => {
        stop();
        if (reduced.matches ||
            document.hidden ||
            !visible ||
            typeof img.animate !== "function")
            return;
        button.dataset.animating = "true";
        animation = img.animate([
            { transform: "perspective(500px) rotateY(0deg) rotateX(0deg)" },
            {
                transform: "perspective(500px) rotateY(-16deg) rotateX(5deg)",
                offset: 0.3,
            },
            {
                transform: "perspective(500px) rotateY(13deg) rotateX(-3deg)",
                offset: 0.7,
            },
            { transform: "perspective(500px) rotateY(0deg) rotateX(0deg)" },
        ], { duration: 1800, easing: "ease-in-out", iterations: 1 });
        animation.onfinish = stop;
    };
    document.addEventListener("visibilitychange", () => {
        if (document.hidden)
            stop();
    });
    reduced.addEventListener("change", stop);
    new IntersectionObserver((entries) => {
        visible = entries[0]?.isIntersecting ?? false;
        if (!visible)
            stop();
    }).observe(button);
}
