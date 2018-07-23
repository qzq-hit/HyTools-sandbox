import argparse,gdal,copy,sys,warnings
import numpy as np, os,pandas as pd,glob,json
import hytools as ht
from hytools.brdf import *
from hytools.topo_correction import *
from hytools.helpers import *
from hytools.preprocess.resampling import *
from hytools.preprocess.vector_norm import *
from hytools.file_io import array_to_geotiff,writeENVI
home = os.path.expanduser("~")

warnings.filterwarnings("ignore")

def progbar(curr, total, full_progbar):
    frac = curr/total
    filled_progbar = round(frac*full_progbar)
    print('\r', '#'*filled_progbar + '-'*(full_progbar-filled_progbar), '[{:>7.2%}]'.format(frac), end='')

def main():
    '''
    Perform in-memory trait estimation.
    '''
    
    parser = argparse.ArgumentParser(description = "In memory trait mapping tool.")
    parser.add_argument("--img", help="Input image pathname",required=True, type = str)
    parser.add_argument("--obs", help="Input observables pathname", required=False, type = str)
    parser.add_argument("--out", help="Output full corrected image", required=False, type = str)
    parser.add_argument("--od", help="Output directory for all resulting products", required=True, type = str)
    parser.add_argument("--brdf", help="Perform BRDF correction",action='store_true')
    parser.add_argument("--kernels", help="Li and Ross kernel types",nargs = 2, type =str)
    parser.add_argument("--topo", help="Perform topographic correction", action='store_true')
    parser.add_argument("--mask", help="Image mask type to use", action='store_true')
    parser.add_argument("--mask_threshold", help="Mask threshold value", type = float)
    parser.add_argument("--rgbim", help="Export RGBI +Mask image.", action='store_true')
    parser.add_argument("--coeffs", help="Trait coefficients directory", required=True, type = str)
    args = parser.parse_args()

    if args.brdf:
       li,ross =  args.kernels
    
    traits = glob.glob("%s/*.json" % args.coeffs)
    
    #Load data objects memory
    if args.img.endswith(".h5"):
        hyObj = ht.openHDF(args.img,load_obs = True)
    else:
        hyObj = ht.openENVI(args.img)
    if args.topo | args.brdf:
        hyObj.load_obs(args.obs)
    if not args.od.endswith("/"):
        args.od+="/"
    if len(hyObj.bad_bands) == 0:   
        hyObj.create_bad_bands([[300,400],[1330,1430],[1800,1960],[2450,2600]])
    hyObj.load_data()
    
    # Generate mask
    if args.mask:
        ir = hyObj.get_wave(850)
        red = hyObj.get_wave(665)
        ndvi = (ir-red)/(ir+red)
        mask = (ndvi > args.mask_threshold) & (ir != hyObj.no_data)
        hyObj.mask = mask 
        del ir,red,ndvi
    else:
        hyObj.mask = np.ones((hyObj.lines,hyObj.columns)).astype(bool)
        print("Warning no mask specified, results may be unreliable!")

    # Generate cosine i and c1 image for topographic correction
    if args.topo:
        cos_i =  calc_cosine_i(hyObj.solar_zn, hyObj.solar_az, hyObj.azimuth , hyObj.slope)
        c1 = np.cos(hyObj.solar_zn) * np.cos( hyObj.slope)
        topo_coeffs= []
           
    # Gernerate scattering kernel images for brdf correction
    if args.brdf:
        k_vol = generate_volume_kernel(hyObj.solar_az,hyObj.solar_zn,hyObj.sensor_az,hyObj.sensor_zn, ross = ross)
        k_geom = generate_geom_kernel(hyObj.solar_az,hyObj.solar_zn,hyObj.sensor_az,hyObj.sensor_zn,li = li)
        k_vol_nadir = generate_volume_kernel(hyObj.solar_az,hyObj.solar_zn,hyObj.sensor_az,0, ross = ross)
        k_geom_nadir = generate_geom_kernel(hyObj.solar_az,hyObj.solar_zn,hyObj.sensor_az,0,li = li)
        brdf_coeffs= []

    # Cycle through the bands and calculate the topographic and BRDF correction coefficients
    print("Calculating image correction coefficients.....")
    iterator = hyObj.iterate(by = 'band')
    while not iterator.complete:   
        band = iterator.read_next() 
        progbar(iterator.current_band+1, len(hyObj.wavelengths), 100)
        #Skip bad bands
        if hyObj.bad_bands[iterator.current_band]:
            # Generate topo correction coefficients
            if args.topo:
                topo_coeff= generate_topo_coeff_band(band,hyObj.mask,cos_i)
                topo_coeffs.append(topo_coeff)
                # Apply topo correction to current band
                correctionFactor = (c1 * topo_coeff)/(cos_i * topo_coeff)
                band = band* correctionFactor
            # Gernerate BRDF correction coefficients
            if args.brdf:
                brdf_coeffs.append(generate_brdf_coeff_band(band,hyObj.mask,k_vol,k_geom))

        if args.topo and iterator.complete:
            topo_df =  pd.DataFrame(topo_coeffs, index=  hyObj.wavelengths[hyObj.bad_bands], columns = ['c'])
            topo_df.to_csv(args.od + os.path.splitext(os.path.basename(args.img))[0]+ "_topo_coefficients.csv")    
            
            
        if args.brdf and iterator.complete:
            brdf_df =  pd.DataFrame(brdf_coeffs,index = hyObj.wavelengths[hyObj.bad_bands],columns=['k_vol','k_geom','k_iso'])
            brdf_df.to_csv(args.od + os.path.splitext(os.path.basename(args.img))[0]+ "_brdf_coefficients.csv")  
        
    print()
        
    #Cycle through the chunks and apply topo, brdf, vnorm,resampling and trait estimation steps
    print("Calculating values for %s traits....." % len(traits))
    pixels_processed = 0
    iterator = hyObj.iterate(by = 'chunk',chunk_size = (100,100))
    
    while not iterator.complete:  
        chunk = iterator.read_next()  
        chunk_nodata_mask = chunk[:,:,1] == hyObj.no_data
        pixels_processed += chunk.shape[0]*chunk.shape[1]
        progbar(pixels_processed, hyObj.columns*hyObj.lines, 100)

        # Chunk Array indices
        line_start =iterator.current_line 
        line_end = iterator.current_line + chunk.shape[0]
        col_start = iterator.current_column
        col_end = iterator.current_column + chunk.shape[1]
        
        # Apply TOPO correction 
        if args.topo:
            cos_i_chunk = cos_i[line_start:line_end,col_start:col_end]
            c1_chunk = c1[line_start:line_end,col_start:col_end]
            correctionFactor = (c1_chunk[:,:,np.newaxis]+topo_df.c.values)/(cos_i_chunk[:,:,np.newaxis] + topo_df.c.values)
            chunk = chunk[:,:,hyObj.bad_bands]* correctionFactor
        else:
            chunk = chunk[:,:,hyObj.bad_bands] *1
        
        # Apply BRDF correction 
        if args.brdf:
            # Get scattering kernel for chunks
            k_vol_nadir_chunk = k_vol_nadir[line_start:line_end,col_start:col_end]
            k_geom_nadir_chunk = k_geom_nadir[line_start:line_end,col_start:col_end]
            k_vol_chunk = k_vol[line_start:line_end,col_start:col_end]
            k_geom_chunk = k_geom[line_start:line_end,col_start:col_end]
    
            # Apply brdf correction 
            # eq 5. Weyermann et al. IEEE-TGARS 2015)
            brdf = np.einsum('i,jk-> jki', brdf_df.k_vol,k_vol_chunk) + np.einsum('i,jk-> jki', brdf_df.k_geom,k_geom_chunk)  + brdf_df.k_iso.values
            brdf_nadir = np.einsum('i,jk-> jki', brdf_df.k_vol,k_vol_nadir_chunk) + np.einsum('i,jk-> jki', brdf_df.k_geom,k_geom_nadir_chunk)  + brdf_df.k_iso.values
            correctionFactor = brdf_nadir/brdf
            chunk= chunk* correctionFactor
        
        #Reassign no data values
        chunk[chunk_nodata_mask,:] = 0
        
        # Export RGBIM image
        if args.rgbim:
            dstFile = args.od + os.path.splitext(os.path.basename(args.img))[0] + '_rgbim.tif'
            if line_start + col_start == 0:
                driver = gdal.GetDriverByName("GTIFF")
                tiff = driver.Create(dstFile,hyObj.columns,hyObj.lines,5,gdal.GDT_Float32)
                tiff.SetGeoTransform(hyObj.transform)
                tiff.SetProjection(hyObj.projection)
                for band in range(1,6):
                    tiff.GetRasterBand(band).SetNoDataValue(0)
                tiff.GetRasterBand(5).WriteArray(hyObj.mask)

                del tiff,driver
            # Write rgbi chunk
            rgbi_geotiff = gdal.Open(dstFile, gdal.GA_Update)
            for i,wave in enumerate([480,560,660,850],start=1):
                    band = hyObj.wave_to_band(wave)
                    rgbi_geotiff.GetRasterBand(i).WriteArray(chunk[:,:,band], col_start, line_start)
            rgbi_geotiff = None
        
        # Export BRDF and topo corrected image
        if args.out:
            if line_start + col_start == 0:
                output_name = args.od + os.path.splitext(os.path.basename(args.img))[0] + "_topo_brdf" 
                header_dict =hyObj.header_dict
                # Update header
                header_dict['wavelength']= header_dict['wavelength'][hyObj.bad_bands]
                header_dict['fwhm'] = header_dict['fwhm'][hyObj.bad_bands]
                header_dict['bbl'] = header_dict['bbl'][hyObj.bad_bands]
                header_dict['bands'] = hyObj.bad_bands.sum()
                writer = writeENVI(output_name,header_dict)
            writer.write_chunk(chunk,iterator.current_line,iterator.current_column)
            if iterator.complete:
                writer.close()

        # Cycle through trait models 
        for i,trait in enumerate(traits):
            dstFile = args.od + os.path.splitext(os.path.basename(args.img))[0] +"_" +os.path.splitext(os.path.basename(trait))[0] +".tif"
            
            # Trait estimation preparation
            if line_start + col_start == 0:
                
                with open(trait) as json_file:  
                    trait_model = json.load(json_file)
                    
                # Check if wavelength units match
                if trait_model['wavelength_units'] == 'micrometers':
                    trait_wave_scaler = 10**3
                else:
                    trait_wave_scaler = 1
                    
                intercept = np.array(trait_model['intercept'])
                coefficients = np.array(trait_model['coefficients'])
                
                # Get list of wavelengths to compare against image wavelengths
                if len(trait_model['vector_norm_wavelengths']) == 0:
                    dst_waves = np.array(trait_model['model_wavelengths'])*trait_wave_scaler
                else:
                    dst_waves = np.array(trait_model['vector_norm_wavelengths'])*trait_wave_scaler
                
                trait_fwhm = np.array(trait_model['fwhm'])* trait_wave_scaler
                model_waves = np.array(trait_model['model_wavelengths'])* trait_wave_scaler
                trait_band_mask = [x in dst_waves for x in model_waves]
                
                if trait_model['vector_norm']:
                    vnorm_scaler = trait_model["vector_scaler"]
                else:
                    vnorm_scaler = None

                # Check if all bands match in image and coeffs
                if np.sum([x in dst_waves for x in hyObj.wavelengths]) != len(dst_waves):
                    resample = True
                    if type(hyObj.fwhm) == np.ndarray:
                        resampling_coeffs = est_transform_matrix(hyObj.wavelengths[hyObj.bad_bands],dst_waves ,hyObj.fwhm[hyObj.bad_bands],trait_fwhm,1)
                else:
                    resample = False
                    resampling_coeffs = None
        
                # Initialize trait dictionary
                if i == 0:
                    trait_dict = {}
                trait_dict[i] = [coefficients,intercept,trait_model['vector_norm'],vnorm_scaler,trait_band_mask,resample,resampling_coeffs]
        
                # Create geotiff driver
                driver = gdal.GetDriverByName("GTIFF")
                tiff = driver.Create(dstFile,hyObj.columns,hyObj.lines,2,gdal.GDT_Float32)
                tiff.SetGeoTransform(hyObj.transform)
                tiff.SetProjection(hyObj.projection)
                tiff.GetRasterBand(1).SetNoDataValue(0)
                tiff.GetRasterBand(2).SetNoDataValue(0)
                del tiff,driver
            
            coefficients,intercept,vnorm,vnorm_scaler,trait_band_mask,resample,resampling_coeffs = trait_dict[i]

            if resample:            
                chunk_r = np.dot(chunk, resampling_coeffs) 
            if vnorm:            
                chunk_v = vector_normalize_chunk(chunk_r,vnorm_scaler)
            
            trait_mean,trait_std = apply_plsr_chunk(chunk_v[:,:,trait_band_mask],coefficients,intercept)
            
            # Change no data pixel values
            trait_mean[chunk_nodata_mask] = 0
            trait_std[chunk_nodata_mask] = 0

            # Write trait estimate to file
            trait_geotiff = gdal.Open(dstFile, gdal.GA_Update)
            trait_geotiff.GetRasterBand(1).WriteArray(trait_mean, col_start, line_start)
            trait_geotiff.GetRasterBand(2).WriteArray(trait_std, col_start, line_start)
            trait_geotiff = None

if __name__== "__main__":
    main()


#python image_to_traits.py --img /Volumes/ssd/aviris/cloquet_subset --obs /Volumes/ssd/aviris/cloquet_subset_obs --od /Volumes/ssd/hyspex/ --brdf --kernels dense thick --topo --mask --mask_threshold .7 --rgbim --coeffs /Users/adam/Dropbox/projects/hyTools/PLSR_Hyspiri_test


