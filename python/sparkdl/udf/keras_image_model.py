#
# Copyright 2017 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging

from ..graph.builder import GraphFunction, IsolatedSession
from ..graph.pieces import buildSpImageConverter, buildFlattener
from ..image.imageIO import imageSchema
from ..utils import jvmapi as JVMAPI

logger = logging.getLogger('sparkdl')

def registerKerasImageUDF(udf_name, keras_model_or_file_path, preprocessor=None):
    """
    Create a Keras image model as a Spark SQL UDF.
    The function takes a column (formatted in :py:const:`sparkdl.image.imageIO.imageSchema`)
    and produce a prediction as a probability over of a set of known categories.

    .. code-block:: python

        registerKerasImageUDF("udf_name", "path/to/my/keras/model.h5", preprocessor)

    Or, we can provide the model

    .. code-block:: python

        from keras.applications import InceptionV3
        registerKerasImageUDF("udf_name", InceptionV3(weights="imagenet"), preprocessor)    

    The :py:obj:`preprocessor` converts a file path into a image array.
    This function is usually introduced in Keras workflow, as in the following example.

    .. warning:: There is a performance penalty to use a :py:obj:`preprocessor` as it will
                 first convert the image into a file buffer and reloaded back.
                 This provides compatibility with the usual way Keras model input are preprocessed.
                 Please consider directly using Keras/TensorFlow layers for this purpose.

    .. code-block:: python

        def keras_load_img(fpath):
            from keras.preprocessing.image import load_img, img_to_array
            import numpy as np
            from pyspark.sql import Row            
            img = load_img(fpath, target_size=(299, 299))
            return img_to_array(img).astype(np.uint8)

        registerKerasImageUDF("my_inception_udf", InceptionV3(weights="imagenet"), keras_load_img)

    If the `preprocessor` is not provided, we assume the function will be applied to
    a column encoded in 

    :param udf_name: str, name of the UserDefinedFunction
    :param keras_model_file_path: str, path to the HDF5 keras model file
    :param preprocessor: function, optional, a function that 
                         converts image file path to image tensor/ndarray
                         in the correct shape to be served as input to the Keras model
    :return: :py:class:`GraphFunction`, the graph function for the Keras image model
    """
    ordered_udf_names = []
    keras_udf_name = udf_name
    if preprocessor is not None:
        # Spill the image structure to file and reload it 
        # with the user provided preprocessing funcition
        preproc_udf_name = '{}__preprocess'.format(udf_name)
        ordered_udf_names.append(preproc_udf_name)
        JVMAPI.registerUDF(
            preproc_udf_name,
            _serialize_and_reload_with(preprocessor),
            imageSchema)
        keras_udf_name = '{}__model_predict'.format(udf_name)

    stages = [('spimg', buildSpImageConverter("RGB")),
              ('model', GraphFunction.fromKeras(keras_model_or_file_path)),
              ('final', buildFlattener())]
    gfn = GraphFunction.fromList(stages)

    with IsolatedSession() as issn:
        _, fetches = issn.importGraphFunction(gfn, prefix='')
        issn.asUDF(keras_udf_name, fetches)
        ordered_udf_names.append(keras_udf_name)

    if len(ordered_udf_names) > 1:
        msg = "registering pipelined UDF {udf} with stages {udfs}"
        msg = msg.format(udf=udf_name, udfs=ordered_udf_names)
        logger.info(msg)
        JVMAPI.registerPipeline(udf_name, ordered_udf_names)

    return gfn

def _serialize_and_reload_with(preprocessor):
    """
    Load a preprocessor function (image_file_path => image_tensor)
    
    :param preprocessor: function, mapping from image file path to an image tensor
    :return: the UDF preprocessor implementation
    """
    def udf_impl(spimg):
        import numpy as np
        from PIL import Image
        from tempfile import NamedTemporaryFile
        from sparkdl.image.imageIO import imageArrayToStruct, imageType
        
        pil_mode = imageType(spimg).pilMode        
        img_shape = (spimg.width, spimg.height)
        img = Image.frombytes(pil_mode, img_shape, bytes(spimg.data))
        # Warning: must use lossless format to guarantee consistency
        temp_fp = NamedTemporaryFile(suffix='.png')
        img.save(temp_fp, 'PNG')
        img_arr_reloaded = preprocessor(temp_fp.name)
        assert isinstance(img_arr_reloaded, np.ndarray), \
            "expect preprocessor to return a numpy array"        
        img_arr_reloaded = img_arr_reloaded.astype(np.uint8)
        return imageArrayToStruct(img_arr_reloaded)

    return udf_impl
