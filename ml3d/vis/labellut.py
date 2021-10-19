class LabelLUT:
    """The class to manage look-up table for assigning colors to labels."""

    class Label:

        def __init__(self, name, value, color):
            self.name = name
            self.value = value
            self.color = color

    COLORS = {
        'default': [[0., 0., 0.], [0.96078431, 0.58823529, 0.39215686],
                    [0.96078431, 0.90196078, 0.39215686],
                    [0.58823529, 0.23529412, 0.11764706],
                    [0.70588235, 0.11764706, 0.31372549], [1., 0., 0.],
                    [0.11764706, 0.11764706, 1.], [0.78431373, 0.15686275, 1.],
                    [0.35294118, 0.11764706, 0.58823529], [1., 0., 1.],
                    [1., 0.58823529, 1.], [0.29411765, 0., 0.29411765],
                    [0.29411765, 0., 0.68627451], [0., 0.78431373, 1.],
                    [0.19607843, 0.47058824, 1.], [0., 0.68627451, 0.],
                    [0., 0.23529412, 0.52941176],
                    [0.31372549, 0.94117647, 0.58823529],
                    [0.58823529, 0.94117647, 1.],
                    [0., 0., 1.], [1.0, 1.0, 0.25], [0.5, 1.0, 0.25],
                    [0.25, 1.0, 0.25], [0.25, 1.0, 0.5], [0.25, 1.0, 1.25],
                    [0.25, 0.5, 1.25], [0.25, 0.25, 1.0], [0.125, 0.125, 0.125],
                    [0.25, 0.25, 0.25], [0.375, 0.375, 0.375], [0.5, 0.5, 0.5],
                    [0.625, 0.625, 0.625], [0.75, 0.75, 0.75],
                    [0.875, 0.875, 0.875]],

        # Categorical 12 color palette from Adobe Spectrum
        # https://spectrum.adobe.com/page/color-for-data-visualization/#Options
        'spectrum':
            [[0.0, 0.7529411764705882, 0.7803921568627451],
             [0.3176470588235294, 0.26666666666666666, 0.8274509803921568],
             [0.9098039215686274, 0.5294117647058824, 0.10196078431372549],
             [0.8549019607843137, 0.20392156862745098, 0.5647058823529412],
             [0.5647058823529412, 0.5372549019607843, 0.9803921568627451],
             [0.2784313725490196, 0.8862745098039215, 0.43529411764705883],
             [0.15294117647058825, 0.5019607843137255, 0.9215686274509803],
             [0.43529411764705883, 0.2196078431372549, 0.6941176470588235],
             [0.8745098039215686, 0.7490196078431373, 0.011764705882352941],
             [0.796078431372549, 0.43529411764705883, 0.06274509803921569],
             [0.14901960784313725, 0.5529411764705883, 0.4235294117647059],
             [0.6078431372549019, 0.9254901960784314, 0.32941176470588235]]
    }

    def __init__(self, label_to_names=None, colormap='default'):
        self._next_color = 0
        self.labels = {}
        self.colors = self.COLORS[colormap]
        if label_to_names is not None:
            for val in sorted(label_to_names.keys()):
                self.add_label(label_to_names[val], val)

    def add_label(self, name, value, color=None):
        """Adds a label to the table.

        **Example:**
            The following sample creates a LUT with 3 labels::

                lut = ml3d.vis.LabelLUT()
                lut.add_label('one', 1)
                lut.add_label('two', 2)
                lut.add_label('three', 3, [0,0,1]) # use blue for label 'three'

        **Args:**
            name: The label name as string.
            value: The value associated with the label.
            color: Optional RGB color. E.g., [0.2, 0.4, 1.0].
        """
        if color is None:
            if self._next_color >= len(self.colors):
                color = [0.85, 1.0, 1.0]
            else:
                color = self.colors[self._next_color]
                self._next_color += 1
        self.labels[value] = self.Label(name, value, color)
